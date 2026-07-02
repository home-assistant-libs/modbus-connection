"""tmodbus-backed implementation of the modbus_connection Protocols.

Mirrors the pymodbus backend over tmodbus. Per the design, three function codes
have no tmodbus equivalent and raise ``NotImplementedError``: diagnostics (0x08),
get-comm-event-counter (0x0B), and get-comm-event-log (0x0C).

tmodbus ships no UDP or TLS transport, so ``connect_udp`` / ``connect_tls`` raise
``NotImplementedError`` — use the pymodbus backend for those.

Requires the ``[tmodbus]`` extra.
"""

from __future__ import annotations

import functools
import ssl
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Concatenate

from tmodbus import (
    AsyncModbusClient,
    create_async_ascii_client,
    create_async_rtu_client,
    create_async_rtu_over_tcp_client,
    create_async_tcp_client,
)
from tmodbus.exceptions import (
    InvalidResponseError,
    ModbusResponseError,
    RequestRetryFailedError,
    TModbusError,
)
from tmodbus.exceptions import (
    ModbusConnectionError as TModbusConnectionError,
)

from .._callbacks import CallbackRegistry
from .._types import SerialFraming, SocketFraming
from ..exceptions import (
    ModbusConnectionError,
    ModbusError,
    ModbusExceptionError,
    ModbusProtocolError,
    ModbusTimeoutError,
)

__all__ = [
    "TmodbusConnection",
    "TmodbusUnit",
    "connect_serial",
    "connect_tcp",
    "connect_tls",
    "connect_udp",
]

# tmodbus binds a unit id when the client is created, but this library selects the
# unit via ``ModbusConnection.for_unit()`` instead. The base client is only used
# to derive per-unit handles (``for_unit_id``), so its own binding is never used
# for I/O; we give it a fixed placeholder that ``for_unit`` always overrides.
_PLACEHOLDER_UNIT_ID = 1


class TmodbusConnection:
    """A live tmodbus connection.

    Inter-request spacing (``message_spacing``) is the transport's own job here:
    it maps to tmodbus's native ``wait_between_requests``, enforced inside the
    client's communication lock — so this wrapper carries no pacing state.

    ``on_connection_lost`` fires at most once per connection. tmodbus exposes no
    transport-level disconnect hook, so a drop is detected *reactively* — on the
    next request that fails — rather than proactively like the pymodbus backend;
    an idle link that drops is not noticed until the next request. Since the
    connection never self-reconnects (the owner builds a new one on loss), the
    first detected failure fires the callbacks and later failures are suppressed.
    """

    def __init__(self, client: AsyncModbusClient) -> None:
        self._client = client
        self._lost_callbacks = CallbackRegistry()
        self._lost_fired = False

    @property
    def connected(self) -> bool:
        return self._client.connected

    def for_unit(self, unit_id: int) -> TmodbusUnit:
        return TmodbusUnit(self, self._client.for_unit_id(unit_id))

    def on_connection_lost(self, callback: Callable[[], None]) -> Callable[[], None]:
        return self._lost_callbacks.subscribe(callback)

    async def close(self) -> None:
        try:
            await self._client.disconnect()
        except (TModbusError, OSError) as err:
            raise ModbusConnectionError(str(err)) from err

    def _notify_lost(self) -> None:
        # A request failed with a connection error. The link does not revive
        # itself, so fire once and suppress the repeats from later failed requests.
        if self._lost_fired:
            return
        self._lost_fired = True
        self._lost_callbacks.fire()


async def _open(
    make_client: Callable[[], AsyncModbusClient],
    error_message: str,
) -> TmodbusConnection:
    """Construct and connect a tmodbus client, wrapping the result.

    Maps every backend failure onto the neutral hierarchy so callers never see
    a raw tmodbus exception: a ``TimeoutError`` (the connect attempt did not
    complete in time) stays a timeout, mirroring the operational path; every
    other transport failure — a raising constructor or a raising ``connect()`` —
    becomes ``ModbusConnectionError``.
    """
    try:
        client = make_client()
        await client.connect()
    except TimeoutError as err:
        raise ModbusTimeoutError(str(err)) from err
    except (TModbusError, OSError) as err:
        raise ModbusConnectionError(error_message) from err
    return TmodbusConnection(client)


def _map_errors[**P, R](
    func: Callable[Concatenate[TmodbusUnit, P], Awaitable[R]],
) -> Callable[Concatenate[TmodbusUnit, P], Coroutine[Any, Any, R]]:
    """Map tmodbus exceptions onto the neutral hierarchy.

    Decorates ``TmodbusUnit`` methods so each body just calls the client
    directly; a connection-lost error also fires the owner's lost callbacks.
    Inter-request spacing is handled by the client itself (see
    ``TmodbusConnection``), so there is nothing to do here.
    """

    @functools.wraps(func)
    async def wrapper(self: TmodbusUnit, *args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await func(self, *args, **kwargs)
        except TModbusConnectionError as err:
            self._conn._notify_lost()
            raise ModbusConnectionError(str(err)) from err
        except (TimeoutError, RequestRetryFailedError) as err:
            raise ModbusTimeoutError(str(err)) from err
        except InvalidResponseError as err:
            # A reply arrived but was not a valid frame (bad CRC/LRC, framing, or a
            # mismatched header) — a protocol error, not a timeout. tmodbus 0.4.0
            # standardized this as a single ``InvalidResponseError``.
            raise ModbusProtocolError(str(err)) from err
        except ModbusResponseError as err:
            raise ModbusExceptionError(int(err.error_code)) from err
        except TModbusError as err:
            raise ModbusError(str(err)) from err

    return wrapper


class TmodbusUnit:
    """A stateless per-unit handle over a unit-bound tmodbus client."""

    def __init__(
        self, connection: TmodbusConnection, client: AsyncModbusClient
    ) -> None:
        self._conn = connection
        self._client = client

    @property
    def connected(self) -> bool:
        return self._conn.connected

    # -- raw register I/O -----------------------------------------------------

    @_map_errors
    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        return await self._client.read_holding_registers(address, count)

    @_map_errors
    async def read_input_registers(self, address: int, count: int) -> list[int]:
        return await self._client.read_input_registers(address, count)

    @_map_errors
    async def write_register(self, address: int, value: int) -> None:
        await self._client.write_single_register(address, value)

    @_map_errors
    async def write_registers(self, address: int, values: list[int]) -> None:
        await self._client.write_multiple_registers(address, values)

    # -- raw coil / discrete-input I/O ----------------------------------------

    @_map_errors
    async def read_coils(self, address: int, count: int) -> list[bool]:
        return await self._client.read_coils(address, count)

    @_map_errors
    async def read_discrete_inputs(self, address: int, count: int) -> list[bool]:
        return await self._client.read_discrete_inputs(address, count)

    @_map_errors
    async def write_coil(self, address: int, value: bool) -> None:
        await self._client.write_single_coil(address, value)

    @_map_errors
    async def write_coils(self, address: int, values: list[bool]) -> None:
        await self._client.write_multiple_coils(address, values)

    # -- full function-code surface -------------------------------------------

    @_map_errors
    async def read_exception_status(self) -> int:  # 0x07
        return int(await self._client.read_exception_status())

    @_map_errors
    async def report_server_id(self) -> bytes:  # 0x11
        response = await self._client.read_server_id()
        return bytes(response.server_id)

    @_map_errors
    async def mask_write_register(
        self, address: int, and_mask: int, or_mask: int
    ) -> None:  # 0x16
        await self._client.mask_write_register(address, and_mask, or_mask)

    @_map_errors
    async def read_write_registers(
        self,
        read_address: int,
        read_count: int,
        write_address: int,
        write_values: list[int],
    ) -> list[int]:  # 0x17
        return await self._client.read_write_multiple_registers(
            read_address, read_count, write_address, write_values
        )

    @_map_errors
    async def read_fifo_queue(self, address: int) -> list[int]:  # 0x18
        return await self._client.read_fifo_queue(address)

    @_map_errors
    async def read_device_identification(self) -> dict[int, bytes]:  # 0x2B / 0x0E
        return await self._client.read_device_identification(1, 0)

    @_map_errors
    async def read_file_record(
        self, file: int, record: int, length: int
    ) -> list[int]:  # 0x14
        # read_file_record returns the record's raw data bytes (big-endian words).
        data = await self._client.read_file_record(file, record, length)
        return [int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data), 2)]

    @_map_errors
    async def write_file_record(
        self, file: int, record: int, values: list[int]
    ) -> None:  # 0x15
        payload = b"".join(int(value).to_bytes(2, "big") for value in values)
        await self._client.write_file_record(file, record, payload)

    async def diagnostics(self, sub_function: int, data: int = 0) -> int:  # 0x08
        raise NotImplementedError("tmodbus does not implement diagnostics (FC 0x08)")

    async def get_comm_event_counter(self) -> tuple[int, int]:  # 0x0B
        raise NotImplementedError(
            "tmodbus does not implement get-comm-event-counter (FC 0x0B)"
        )

    async def get_comm_event_log(self) -> bytes:  # 0x0C
        raise NotImplementedError(
            "tmodbus does not implement get-comm-event-log (FC 0x0C)"
        )

    def on_connection_lost(self, callback: Callable[[], None]) -> Callable[[], None]:
        return self._conn.on_connection_lost(callback)


async def connect_tcp(
    host: str,
    *,
    port: int = 502,
    timeout: float = 3,
    framer: SocketFraming = "socket",
    message_spacing: float = 0.0,
) -> TmodbusConnection:
    """Open a Modbus TCP / RTU-over-TCP connection over tmodbus.

    ``framer`` selects the wire framing: ``"socket"`` for native Modbus TCP
    (MBAP), or ``"rtu"`` for RTU-over-TCP — what transparent serial-to-Ethernet
    gateways speak. ``"ascii"`` (ASCII-over-TCP) raises ``NotImplementedError``:
    tmodbus has no ASCII-over-TCP transport — use the pymodbus backend.

    ``message_spacing`` is the minimum gap, in seconds, left after each request
    before the next may start — applied across every unit sharing the link, via
    tmodbus's native ``wait_between_requests``. Use it for devices that need a
    pause between frames; ``0`` (the default) disables it.

    ``auto_reconnect`` is disabled: on loss the owner recreates the connection.
    Raises ``ModbusConnectionError`` if the connection cannot be established.
    """
    if framer == "socket":
        create = create_async_tcp_client
    elif framer == "rtu":
        create = create_async_rtu_over_tcp_client
    elif framer == "ascii":
        raise NotImplementedError(
            "tmodbus has no ASCII-over-TCP transport; use the pymodbus backend"
        )
    else:
        raise ValueError(
            f"unknown framer {framer!r}; expected 'socket', 'rtu', or 'ascii'"
        )
    return await _open(
        lambda: create(
            host,
            port,
            unit_id=_PLACEHOLDER_UNIT_ID,
            timeout=timeout,
            auto_reconnect=False,
            wait_between_requests=message_spacing,
        ),
        f"could not connect to {host}:{port}",
    )


async def connect_udp(
    host: str,
    *,
    port: int = 502,
    timeout: float = 3,
    framer: SocketFraming = "socket",
    message_spacing: float = 0.0,
) -> TmodbusConnection:
    """Modbus UDP is not available over tmodbus.

    tmodbus ships no UDP transport, so this always raises
    ``NotImplementedError``. Use ``modbus_connection.pymodbus.connect_udp`` for
    Modbus UDP. Kept here so the backend's connect surface mirrors pymodbus's.
    """
    raise NotImplementedError("tmodbus has no UDP transport; use the pymodbus backend")


async def connect_tls(
    host: str,
    *,
    port: int = 802,
    sslctx: ssl.SSLContext | None = None,
    certfile: str | None = None,
    keyfile: str | None = None,
    password: str | None = None,
    timeout: float = 3,
    message_spacing: float = 0.0,
) -> TmodbusConnection:
    """Modbus/TLS is not available over tmodbus.

    tmodbus ships no TLS transport, so this always raises
    ``NotImplementedError``. Use ``modbus_connection.pymodbus.connect_tls`` for
    Modbus/TLS. Kept here so the backend's connect surface mirrors pymodbus's.
    """
    raise NotImplementedError("tmodbus has no TLS transport; use the pymodbus backend")


async def connect_serial(
    port: str,
    *,
    baudrate: int = 9600,
    bytesize: int = 8,
    parity: str = "N",
    stopbits: int = 1,
    framer: SerialFraming = "rtu",
    message_spacing: float = 0.0,
) -> TmodbusConnection:
    """Open a Modbus serial connection over tmodbus and return a live handle.

    ``framer`` selects the serial framing: ``"rtu"`` for binary Modbus RTU (the
    default) or ``"ascii"`` for the ASCII transmission mode.

    ``message_spacing`` is the minimum gap, in seconds, left after each request
    before the next may start (see ``connect_tcp``); ``0`` (the default) disables
    it.

    ``auto_reconnect`` is disabled. Raises ``ModbusConnectionError`` on failure.
    """
    if framer == "rtu":
        create = create_async_rtu_client
    elif framer == "ascii":
        create = create_async_ascii_client
    else:
        raise ValueError(f"unknown serial framer {framer!r}; expected 'rtu' or 'ascii'")
    return await _open(
        lambda: create(
            port,
            unit_id=_PLACEHOLDER_UNIT_ID,
            baudrate=baudrate,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            auto_reconnect=False,
            wait_between_requests=message_spacing,
        ),
        f"could not open serial port {port}",
    )
