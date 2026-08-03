"""Shared test fixtures: a real in-process Modbus server.

Both backends connect to the *same* tmodbus server, so the test suite validates
real end-to-end behavior and cross-backend parity rather than mock interactions.
Running pymodbus's client against tmodbus's server also keeps the
cross-implementation coverage that a same-library loop would lose.
"""

from __future__ import annotations

import asyncio
import socket
import ssl
import struct
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from dataclasses import dataclass, field

import pytest
from tmodbus.exceptions import IllegalDataAddressError
from tmodbus.pdu import (
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

from modbus_connection import ModbusConnection

UNIT_ID = 1

# Known holding-register contents, shared by the raw-read and parity tests.
HOLDING: dict[int, int] = {0: 1234, 1: 0xFFFF}
HOLDING[2], HOLDING[3] = (70000 >> 16) & 0xFFFF, 70000 & 0xFFFF
_f = struct.unpack(">HH", struct.pack(">f", 12.5))
HOLDING[4], HOLDING[5] = _f[0], _f[1]
for i, ch in enumerate(b"ABCDEF\x00\x00"):
    reg, hi = divmod(i, 2)
    HOLDING.setdefault(6 + reg, 0)
    HOLDING[6 + reg] |= ch << (8 if hi == 0 else 0)

INPUT: dict[int, int] = {0: 555, 1: 777}
COILS: dict[int, bool] = {0: True, 1: False, 2: True, 56: True}
DISCRETE: dict[int, bool] = {0: False, 1: True, 2: True}

# Device identification (FC43/14) the server advertises, keyed by MEI object id
# (0 VendorName, 1 ProductCode, 2 MajorMinorRevision).
DEVICE_ID: dict[int, bytes] = {0: b"Acme", 1: b"PC-1", 2: b"1.2"}

# Addresses past the end of an address space answer with an exception, so make
# the spaces comfortably larger than any address the tests touch.
_SPACE_SIZE = 2200

_StreamHandler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


@dataclass
class Datastore:
    """The four Modbus address spaces a test server answers from.

    Reads and writes past ``size`` raise ``IllegalDataAddressError``, which is
    what lets the tests assert on exception code 2.
    """

    holding: dict[int, int] = field(default_factory=dict)
    input: dict[int, int] = field(default_factory=dict)
    coils: dict[int, bool] = field(default_factory=dict)
    discrete_inputs: dict[int, bool] = field(default_factory=dict)
    device_id: dict[int, bytes] = field(default_factory=dict)
    size: int = _SPACE_SIZE

    def check_range(self, function_code: int, address: int, count: int) -> None:
        if address < 0 or address + count > self.size:
            raise IllegalDataAddressError(function_code)


def holding_store(values: list[int]) -> Datastore:
    """A datastore serving ``values`` as holding registers 0..len-1.

    The shared datastore for the transport tests (UDP, serial, TLS, framer
    variants), which only need a couple of known holding registers.
    """
    return Datastore(holding={a: v & 0xFFFF for a, v in enumerate(values)})


def build_router(store: Datastore) -> ModbusRequestRouter:
    """Route the function codes the tests exercise onto ``store``.

    Handlers are registered without a unit id, so one datastore answers for
    every unit id the tests address.
    """
    router = ModbusRequestRouter()

    @router.register(ReadHoldingRegistersPDU)
    async def read_holding(unit_id: int, request: ReadHoldingRegistersPDU) -> list[int]:
        store.check_range(
            request.function_code, request.start_address, request.quantity
        )
        return [
            store.holding.get(request.start_address + i, 0)
            for i in range(request.quantity)
        ]

    @router.register(ReadInputRegistersPDU)
    async def read_input(unit_id: int, request: ReadInputRegistersPDU) -> list[int]:
        store.check_range(
            request.function_code, request.start_address, request.quantity
        )
        return [
            store.input.get(request.start_address + i, 0)
            for i in range(request.quantity)
        ]

    @router.register(ReadCoilsPDU)
    async def read_coils(unit_id: int, request: ReadCoilsPDU) -> list[bool]:
        store.check_range(
            request.function_code, request.start_address, request.quantity
        )
        return [
            store.coils.get(request.start_address + i, False)
            for i in range(request.quantity)
        ]

    @router.register(ReadDiscreteInputsPDU)
    async def read_discrete(unit_id: int, request: ReadDiscreteInputsPDU) -> list[bool]:
        store.check_range(
            request.function_code, request.start_address, request.quantity
        )
        return [
            store.discrete_inputs.get(request.start_address + i, False)
            for i in range(request.quantity)
        ]

    @router.register(WriteSingleRegisterPDU)
    async def write_register(unit_id: int, request: WriteSingleRegisterPDU) -> int:
        store.check_range(request.function_code, request.address, 1)
        store.holding[request.address] = request.value
        return request.value

    @router.register(WriteMultipleRegistersPDU)
    async def write_registers(unit_id: int, request: WriteMultipleRegistersPDU) -> int:
        store.check_range(
            request.function_code, request.start_address, len(request.values)
        )
        for offset, value in enumerate(request.values):
            store.holding[request.start_address + offset] = value
        return len(request.values)

    @router.register(WriteSingleCoilPDU)
    async def write_coil(unit_id: int, request: WriteSingleCoilPDU) -> bool:
        store.check_range(request.function_code, request.address, 1)
        store.coils[request.address] = request.value
        return request.value

    @router.register(WriteMultipleCoilsPDU)
    async def write_coils(unit_id: int, request: WriteMultipleCoilsPDU) -> int:
        # WriteMultipleCoilsPDU names its start address ``address``.
        store.check_range(request.function_code, request.address, len(request.values))
        for offset, value in enumerate(request.values):
            store.coils[request.address + offset] = value
        return len(request.values)

    @router.register(ReadDeviceIdentificationPDU)
    async def read_device_id(
        unit_id: int, request: ReadDeviceIdentificationPDU
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

    return router


def full_store() -> Datastore:
    """The datastore the end-to-end and parity tests read known values from."""
    return Datastore(
        holding=dict(HOLDING),
        input=dict(INPUT),
        coils=dict(COILS),
        discrete_inputs=dict(DISCRETE),
        device_id=dict(DEVICE_ID),
    )


@asynccontextmanager
async def serve_stream(
    handle_client: _StreamHandler,
    host: str,
    port: int,
    *,
    ssl_context: ssl.SSLContext | None = None,
) -> AsyncIterator[None]:
    """Serve ``handle_client`` on ``host``/``port`` for the duration of the block.

    ``asyncio.start_server`` returns once the listener is bound and accepting,
    and ``wait_closed()`` returns once it is gone, so neither side needs polling
    or a sleep.

    The listener is ours rather than the tmodbus server's own ``start()`` /
    ``stop()`` so that lingering client sockets can be aborted on the way out.
    Tests that simulate a lost link fire the backend's connection-lost hook
    while the socket is still open, and since Python 3.12 ``wait_closed()``
    blocks until every accepted connection is done — which such a socket never
    is. tmodbus servers keep the connection handling in ``handle_client``, so
    only the listener lifecycle changes.
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
    """Run a Modbus TCP server answering from ``router`` on host/port.

    Tests that need a device to misbehave (answer busy, fail, stall) register
    their own handlers instead of answering from a datastore.
    """
    server = AsyncTcpServer(host, router, port)
    return serve_stream(server.handle_client, host, port, ssl_context=ssl_context)


def serve_tcp(
    store: Datastore,
    host: str,
    port: int,
    *,
    ssl_context: ssl.SSLContext | None = None,
) -> AbstractAsyncContextManager[None]:
    """Run a Modbus TCP server (mbaps when ``ssl_context`` is set) on host/port."""
    return serve_router(build_router(store), host, port, ssl_context=ssl_context)


def serve_rtu_over_tcp(
    store: Datastore, host: str, port: int
) -> AbstractAsyncContextManager[None]:
    """Run an RTU-over-TCP server, like a serial-to-Ethernet gateway."""
    server = AsyncRtuOverTcpServer(host, build_router(store), port)
    return serve_stream(server.handle_client, host, port)


@asynccontextmanager
async def serve_udp(store: Datastore, host: str, port: int) -> AsyncIterator[None]:
    """Run a Modbus UDP server on ``host``/``port`` for the duration of the block.

    UDP is connectionless, so the server's own ``start()`` / ``stop()`` bind and
    close the endpoint with nothing to linger.
    """
    server = AsyncUdpServer(host, build_router(store), port)
    await server.start()
    try:
        yield
    finally:
        await server.stop()


@asynccontextmanager
async def serve_serial(
    store: Datastore, port: str, framing: str, *, baudrate: int = 9600
) -> AsyncIterator[None]:
    """Run a Modbus RTU or ASCII server on the serial port ``port``."""
    server_cls = AsyncRtuServer if framing == "rtu" else AsyncAsciiServer
    server = server_cls(port, build_router(store), baudrate)
    await server.start()
    try:
        yield
    finally:
        await server.stop()


async def drop_link(conn: ModbusConnection) -> None:
    """Down a live connection the way a transport drop does.

    Stands in for a real link loss (which both backends report through their
    connection-lost hook): the client is cleared and torn down, so the link is
    down and the next request has to reconnect.
    """
    client, conn._client = conn._client, None
    await conn._close_client(client)


@contextmanager
def _bound_port(kind: int) -> Iterator[int]:
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        yield sock.getsockname()[1]


@pytest.fixture
def free_port() -> int:
    """A port on localhost that nothing is listening on."""
    with _bound_port(socket.SOCK_STREAM) as port:
        return port


@pytest.fixture
def free_udp_port() -> int:
    """A UDP port on localhost that nothing is bound to."""
    with _bound_port(socket.SOCK_DGRAM) as port:
        return port


@pytest.fixture
async def modbus_server(free_port: int) -> AsyncIterator[tuple[str, int]]:
    """Start a Modbus TCP server with the known datastore; yield (host, port)."""
    host, port = "127.0.0.1", free_port
    async with serve_tcp(full_store(), host, port):
        yield host, port
