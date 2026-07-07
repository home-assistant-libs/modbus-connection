"""tmodbus-backed implementation of the modbus_connection Protocols.

Implements the ``ModbusConnection`` / ``ModbusUnit`` Protocols over tmodbus. Per
the design, three function codes have no tmodbus equivalent and raise
``NotImplementedError``: diagnostics (0x08), get-comm-event-counter (0x0B), and
get-comm-event-log (0x0C).

tmodbus ships no UDP transport, so ``connect_udp`` raises ``NotImplementedError``.

Requires the ``[tmodbus]`` extra.
"""

from __future__ import annotations

import asyncio
import functools
import ssl
from collections.abc import Awaitable, Callable
from types import CoroutineType
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
from .._tls import build_tls_context
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

    ``on_connection_lost`` callbacks fire once, as soon as the link drops — even
    while no request is in flight — but not for a deliberate ``close()``.
    """

    def __init__(self, client: AsyncModbusClient) -> None:
        self._client = client
        self._lost_callbacks = CallbackRegistry()
        self._closing = False

    @property
    def connected(self) -> bool:
        return self._client.connected

    def for_unit(self, unit_id: int) -> TmodbusUnit:
        return TmodbusUnit(self, self._client.for_unit_id(unit_id))

    def on_connection_lost(self, callback: Callable[[], None]) -> Callable[[], None]:
        return self._lost_callbacks.subscribe(callback)

    async def close(self) -> None:
        self._closing = True
        try:
            await self._client.disconnect()
        except (TModbusError, OSError) as err:
            raise ModbusConnectionError(str(err)) from err

    def _on_connection_lost(self, exc: Exception | None) -> None:
        # Our own close() also triggers this hook, which is not a lost connection.
        if self._closing:
            return
        self._lost_callbacks.fire()


async def _open(
    make_client: Callable[[Callable[[Exception | None], None]], AsyncModbusClient],
    error_message: str,
) -> TmodbusConnection:
    """Construct and connect a tmodbus client, wrapping the result.

    ``make_client`` receives the connection's ``on_connection_lost`` hook and
    returns the client wired to it.
    """
    connection = TmodbusConnection.__new__(TmodbusConnection)
    try:
        client = make_client(connection._on_connection_lost)
        TmodbusConnection.__init__(connection, client)
        await client.connect()
    except TimeoutError as err:
        raise ModbusTimeoutError(str(err)) from err
    except (TModbusError, OSError) as err:
        raise ModbusConnectionError(error_message) from err
    return connection


def _map_errors[**P, R](
    func: Callable[Concatenate[TmodbusUnit, P], Awaitable[R]],
) -> Callable[Concatenate[TmodbusUnit, P], CoroutineType[Any, Any, R]]:
    """Map tmodbus exceptions onto the neutral hierarchy.

    Decorates ``TmodbusUnit`` methods so each body just calls the client directly.
    """

    @functools.wraps(func)
    async def wrapper(self: TmodbusUnit, *args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await func(self, *args, **kwargs)
        except TModbusConnectionError as err:
            raise ModbusConnectionError(str(err)) from err
        except (TimeoutError, RequestRetryFailedError) as err:
            raise ModbusTimeoutError(str(err)) from err
        except InvalidResponseError as err:
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
    tmodbus has no ASCII-over-TCP transport.

    ``message_spacing`` is the minimum gap, in seconds, left after each request
    before the next may start — applied across every unit sharing the link, via
    tmodbus's native ``wait_between_requests``. Use it for devices that need a
    pause between frames; ``0`` (the default) disables it.

    Raises ``ModbusConnectionError`` if the connection cannot be established.
    """
    if framer == "socket":
        create = create_async_tcp_client
    elif framer == "rtu":
        create = create_async_rtu_over_tcp_client
    elif framer == "ascii":
        raise NotImplementedError("tmodbus has no ASCII-over-TCP transport")
    else:
        raise ValueError(
            f"unknown framer {framer!r}; expected 'socket', 'rtu', or 'ascii'"
        )
    return await _open(
        lambda on_lost: create(
            host,
            port,
            unit_id=_PLACEHOLDER_UNIT_ID,
            timeout=timeout,
            auto_reconnect=False,
            wait_between_requests=message_spacing,
            on_connection_lost=on_lost,
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

    tmodbus ships no UDP transport, so this always raises ``NotImplementedError``.
    Kept here so the backend's connect surface stays complete.
    """
    raise NotImplementedError("tmodbus has no UDP transport")


async def connect_tls(
    host: str,
    *,
    port: int = 802,
    verify: bool | str = True,
    check_hostname: bool = True,
    client_cert: str | None = None,
    client_key: str | None = None,
    client_key_password: str | None = None,
    sslctx: ssl.SSLContext | None = None,
    timeout: float = 3,
    message_spacing: float = 0.0,
) -> TmodbusConnection:
    """Open a Modbus/TLS (Modbus Security) connection over tmodbus.

    The SSL context is handed to ``asyncio.create_connection`` (which uses ``host``
    as the ``server_hostname`` for verification).

    Server verification — ``verify`` controls how the device's certificate is
    checked (the ``httpx`` convention):

    - ``True`` (default) — verify against the system trust store.
    - ``False`` — do not verify, for a device with a self-signed certificate.
    - a path (``str``) — verify against a CA bundle (a file) or a directory of
      CAs, e.g. to pin a device's own self-signed certificate.

    ``check_hostname`` (default ``True``) gates hostname matching while still
    verifying the certificate — set it ``False`` for a device reached by an
    address its certificate has no SAN for; ignored when ``verify`` is ``False``.

    Client identity (mutual TLS) — ``client_cert`` / ``client_key`` /
    ``client_key_password`` are this side's own certificate, presented to the
    device; independent of the server-verification arguments.

    Pass a fully-configured ``sslctx`` to take full control; it overrides every
    argument above.

    ``message_spacing`` is the minimum gap, in seconds, left after each request
    before the next may start (see ``connect_tcp``); ``0`` (the default) disables
    it.

    Raises ``ModbusConnectionError`` if the connection cannot be established.
    """
    context = sslctx or await asyncio.to_thread(
        build_tls_context,
        verify,
        check_hostname,
        client_cert,
        client_key,
        client_key_password,
    )
    return await _open(
        lambda on_lost: create_async_tcp_client(
            host,
            port,
            unit_id=_PLACEHOLDER_UNIT_ID,
            timeout=timeout,
            auto_reconnect=False,
            wait_between_requests=message_spacing,
            ssl=context,
            on_connection_lost=on_lost,
        ),
        f"could not connect to {host}:{port}",
    )


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

    Raises ``ModbusConnectionError`` on failure.
    """
    if framer == "rtu":
        create = create_async_rtu_client
    elif framer == "ascii":
        create = create_async_ascii_client
    else:
        raise ValueError(f"unknown serial framer {framer!r}; expected 'rtu' or 'ascii'")
    return await _open(
        lambda on_lost: create(
            port,
            unit_id=_PLACEHOLDER_UNIT_ID,
            baudrate=baudrate,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            auto_reconnect=False,
            wait_between_requests=message_spacing,
            on_connection_lost=on_lost,
        ),
        f"could not open serial port {port}",
    )
