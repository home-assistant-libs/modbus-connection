"""Run a real in-process Modbus server for the tests, backed by tmodbus.

A ``Datastore`` holds the four address spaces, ``build_router`` maps the
function codes the tests exercise onto it, and the ``serve_*`` helpers run it
over one transport each. Tests that need a device to *misbehave* build their own
router instead and pass it to ``serve_router``.
"""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from tmodbus.exceptions import IllegalDataAddressError, IllegalFunctionError
from tmodbus.pdu import (
    BaseDiagnosticsSubFunctionPDU,
    CommEventCounterResponse,
    CommEventLogResponse,
    DiagnosticsBusMessageCountPDU,
    DiagnosticsQueryDataPDU,
    GetCommEventCounterPDU,
    GetCommEventLogPDU,
    ReadCoilsPDU,
    ReadDeviceIdentificationPDU,
    ReadDeviceIdentificationResponse,
    ReadDiscreteInputsPDU,
    ReadHoldingRegistersPDU,
    ReadInputRegistersPDU,
    WriteMultipleCoilsPDU,
    WriteMultipleRegistersPDU,
    WriteSingleCoilPDU,
    WriteSingleRegisterPDU,
)
from tmodbus.pdu.device import ConformityLevel
from tmodbus.server import (
    AsyncAsciiServer,
    AsyncRtuOverTcpServer,
    AsyncRtuServer,
    AsyncTcpServer,
    AsyncUdpServer,
    ModbusRequestRouter,
)

# Addresses past the end of an address space answer with an exception, so make
# the spaces comfortably larger than any address the tests touch.
_SPACE_SIZE = 2200

_StreamHandler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]

# Any read PDU: they all carry a start address and a quantity. The discrete-input
# and input-register PDUs subclass these two.
_ReadPDU = ReadCoilsPDU | ReadHoldingRegistersPDU


@dataclass
class Datastore:
    """The four Modbus address spaces a test server answers from.

    Access past ``size`` raises ``IllegalDataAddressError``, which is what lets
    the tests assert on exception code 2.
    """

    holding: dict[int, int] = field(default_factory=dict)
    input: dict[int, int] = field(default_factory=dict)
    coils: dict[int, bool] = field(default_factory=dict)
    discrete_inputs: dict[int, bool] = field(default_factory=dict)
    device_id: dict[int, bytes] = field(default_factory=dict)
    size: int = _SPACE_SIZE
    bus_message_count: int = 0
    comm_event_log: bytes = b""

    def check_range(self, function_code: int, address: int, count: int) -> None:
        if address < 0 or address + count > self.size:
            raise IllegalDataAddressError(function_code)

    def read[T](self, space: dict[int, T], request: _ReadPDU, default: T) -> list[T]:
        self.check_range(request.function_code, request.start_address, request.quantity)
        return [
            space.get(request.start_address + i, default)
            for i in range(request.quantity)
        ]


def holding_store(values: list[int]) -> Datastore:
    """A datastore serving ``values`` as holding registers 0..len-1."""
    return Datastore(holding={a: v & 0xFFFF for a, v in enumerate(values)})


def build_router(store: Datastore) -> ModbusRequestRouter:
    """Route the function codes the tests exercise onto ``store``.

    Handlers are registered without a unit id, so one datastore answers for
    every unit id the tests address.
    """
    router = ModbusRequestRouter()

    @router.register(ReadHoldingRegistersPDU)
    async def read_holding(uid: int, request: ReadHoldingRegistersPDU) -> list[int]:
        return store.read(store.holding, request, 0)

    @router.register(ReadInputRegistersPDU)
    async def read_input(uid: int, request: ReadInputRegistersPDU) -> list[int]:
        return store.read(store.input, request, 0)

    @router.register(ReadCoilsPDU)
    async def read_coils(uid: int, request: ReadCoilsPDU) -> list[bool]:
        return store.read(store.coils, request, False)

    @router.register(ReadDiscreteInputsPDU)
    async def read_discrete(uid: int, request: ReadDiscreteInputsPDU) -> list[bool]:
        return store.read(store.discrete_inputs, request, False)

    @router.register(WriteSingleRegisterPDU)
    async def write_register(uid: int, request: WriteSingleRegisterPDU) -> int:
        store.check_range(request.function_code, request.address, 1)
        store.holding[request.address] = request.value
        return request.value

    @router.register(WriteMultipleRegistersPDU)
    async def write_registers(uid: int, request: WriteMultipleRegistersPDU) -> int:
        store.check_range(
            request.function_code, request.start_address, len(request.values)
        )
        for offset, value in enumerate(request.values):
            store.holding[request.start_address + offset] = value
        return len(request.values)

    @router.register(WriteSingleCoilPDU)
    async def write_coil(uid: int, request: WriteSingleCoilPDU) -> bool:
        store.check_range(request.function_code, request.address, 1)
        store.coils[request.address] = request.value
        return request.value

    @router.register(WriteMultipleCoilsPDU)
    async def write_coils(uid: int, request: WriteMultipleCoilsPDU) -> int:
        store.check_range(
            request.function_code, request.start_address, len(request.values)
        )
        for offset, value in enumerate(request.values):
            store.coils[request.start_address + offset] = value
        return len(request.values)

    @router.register(ReadDeviceIdentificationPDU)
    async def read_device_id(
        uid: int, request: ReadDeviceIdentificationPDU
    ) -> ReadDeviceIdentificationResponse:
        # Every object fits in one response, so the stream never continues.
        return ReadDeviceIdentificationResponse(
            device_id_code=request.read_device_id_code,
            conformity_level=ConformityLevel.BASIC,
            more=False,
            next_object_id=0,
            number_of_objects=len(store.device_id),
            objects=dict(store.device_id),
        )

    # The router keys handlers by function code alone, so one handler answers
    # every diagnostics sub-function the server decodes.
    @router.register(DiagnosticsQueryDataPDU)
    async def diagnostics(uid: int, request: BaseDiagnosticsSubFunctionPDU[Any]) -> Any:
        if isinstance(request, DiagnosticsQueryDataPDU):
            # Sub-function 0x0000 loops the request data back unchanged.
            return request.data
        if isinstance(request, DiagnosticsBusMessageCountPDU):
            return store.bus_message_count
        raise IllegalFunctionError(request.function_code)

    @router.register(GetCommEventCounterPDU)
    async def get_comm_event_counter(
        uid: int, request: GetCommEventCounterPDU
    ) -> CommEventCounterResponse:
        return CommEventCounterResponse(status=0, event_count=len(store.comm_event_log))

    @router.register(GetCommEventLogPDU)
    async def get_comm_event_log(
        uid: int, request: GetCommEventLogPDU
    ) -> CommEventLogResponse:
        return CommEventLogResponse(
            status=0,
            event_count=len(store.comm_event_log),
            message_count=store.bus_message_count,
            events=store.comm_event_log,
        )

    return router


@asynccontextmanager
async def serve_stream(
    handle_client: _StreamHandler,
    host: str,
    port: int,
    *,
    ssl_context: ssl.SSLContext | None = None,
) -> AsyncIterator[None]:
    """Serve ``handle_client`` on ``host``/``port`` for the duration of the block.

    The listener is ours rather than the tmodbus server's own ``start()`` /
    ``stop()`` so lingering client sockets can be aborted on the way out: tests
    that simulate a lost link fire the connection-lost hook while the socket is
    still open, and since Python 3.12 ``wait_closed()`` blocks until every
    accepted connection is done — which such a socket never is.
    """
    live: set[asyncio.StreamWriter] = set()

    async def track(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        live.add(writer)
        try:
            await handle_client(reader, writer)
        finally:
            live.discard(writer)

    listener = await asyncio.start_server(track, host, port, ssl=ssl_context)
    try:
        yield
    finally:
        listener.close()
        for writer in live:
            writer.transport.abort()
        await listener.wait_closed()


def serve_router(
    router: ModbusRequestRouter,
    host: str,
    port: int,
    *,
    ssl_context: ssl.SSLContext | None = None,
) -> AbstractAsyncContextManager[None]:
    server = AsyncTcpServer(host, router, port=port)
    return serve_stream(server.handle_client, host, port, ssl_context=ssl_context)


def serve_tcp(
    store: Datastore,
    host: str,
    port: int,
    *,
    ssl_context: ssl.SSLContext | None = None,
) -> AbstractAsyncContextManager[None]:
    """Serve ``store`` over Modbus TCP, or mbaps when ``ssl_context`` is set."""
    return serve_router(build_router(store), host, port, ssl_context=ssl_context)


def serve_rtu_over_tcp(
    store: Datastore, host: str, port: int
) -> AbstractAsyncContextManager[None]:
    """Serve ``store`` the way a serial-to-Ethernet gateway does."""
    server = AsyncRtuOverTcpServer(host, build_router(store), port=port)
    return serve_stream(server.handle_client, host, port)


@asynccontextmanager
async def serve_udp(store: Datastore, host: str, port: int) -> AsyncIterator[None]:
    # UDP is connectionless, so nothing can linger and the server's own
    # start()/stop() are enough.
    server = AsyncUdpServer(host, build_router(store), port=port)
    await server.start()
    try:
        yield
    finally:
        await server.stop()


@asynccontextmanager
async def serve_serial(
    store: Datastore,
    port: str,
    framing: Literal["rtu", "ascii"],
    *,
    baudrate: int = 9600,
) -> AsyncIterator[None]:
    server_cls = AsyncRtuServer if framing == "rtu" else AsyncAsciiServer
    server = server_cls(port, build_router(store), baudrate)
    await server.start()
    try:
        yield
    finally:
        await server.stop()
