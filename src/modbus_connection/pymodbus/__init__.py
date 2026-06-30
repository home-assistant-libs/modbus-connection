"""pymodbus-backed implementation of the modbus_connection Protocols.

Provides the connect functions (``connect_tcp`` / ``connect_udp`` /
``connect_serial``) plus the concrete ``PymodbusConnection`` / ``PymodbusUnit``
classes. These are the only backend-specific touchpoints — swapping to tmodbus
changes only the import.

Requires the ``[pymodbus]`` extra.
"""

from __future__ import annotations

import asyncio
import functools
import ssl
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any, Concatenate, Literal

from pymodbus import FramerType
from pymodbus.client import (
    AsyncModbusSerialClient,
    AsyncModbusTcpClient,
    AsyncModbusTlsClient,
    AsyncModbusUdpClient,
)
from pymodbus.client.base import ModbusBaseClient
from pymodbus.exceptions import (
    ConnectionException,
    ModbusException,
    ModbusIOException,
    ParameterException,
)
from pymodbus.pdu import ExceptionResponse, ModbusPDU
from pymodbus.pdu.diag_message import DiagnosticBase
from pymodbus.pdu.file_message import FileRecord

from ..exceptions import (
    ModbusConnectionError,
    ModbusError,
    ModbusExceptionError,
    ModbusTimeoutError,
)

SocketFraming = Literal["socket", "rtu", "ascii"]
SerialFraming = Literal["rtu", "ascii"]

__all__ = [
    "PymodbusConnection",
    "PymodbusUnit",
    "connect_serial",
    "connect_tcp",
    "connect_tls",
    "connect_udp",
]


def _map_errors[**P, R](
    func: Callable[Concatenate[PymodbusUnit, P], Awaitable[R]],
) -> Callable[Concatenate[PymodbusUnit, P], Coroutine[Any, Any, R]]:
    """Map pymodbus transport exceptions onto the neutral hierarchy.

    Also paces the request so a configured inter-request gap is honored across
    every unit on the link.
    """

    @functools.wraps(func)
    async def wrapper(self: PymodbusUnit, *args: P.args, **kwargs: P.kwargs) -> R:
        try:
            async with self._conn._paced():
                return await func(self, *args, **kwargs)
        except ConnectionException as err:
            raise ModbusConnectionError(str(err)) from err
        except ModbusIOException as err:
            raise ModbusTimeoutError(str(err)) from err
        except ModbusException as err:
            raise ModbusError(str(err)) from err

    return wrapper


def _check(response: ModbusPDU) -> ModbusPDU:
    """Raise the neutral error for a pymodbus error-response PDU; else pass it on.

    pymodbus returns decoded error PDUs rather than raising on them, so every
    request must inspect ``isError()`` itself.
    """
    if response.isError():
        if isinstance(response, ExceptionResponse):
            raise ModbusExceptionError(response.exception_code)
        raise ModbusError(f"Modbus request failed: {response}")
    return response


def _safe_close(client: ModbusBaseClient) -> None:
    """Best-effort close used on the connect-failure path; never raises.

    The connection attempt has already failed, so a teardown error here would
    only mask the ``ModbusConnectionError`` we are about to raise.
    """
    try:
        client.close()
    except (ModbusException, OSError):
        pass


def _connect_error(err: Exception, error_message: str) -> Exception:
    """Translate a pymodbus construct/connect failure to the neutral type.

    A ``ParameterException`` means the caller passed bad configuration, not that
    the link is down — surface it as ``ValueError`` (as the framer guards do)
    instead of masking a caller bug as a transient connection failure. A
    ``TimeoutError`` (the connect attempt did not complete in time) stays a
    timeout, mirroring the operational path. Every other transport failure
    becomes ``ModbusConnectionError``.
    """
    if isinstance(err, ParameterException):
        return ValueError(str(err))
    if isinstance(err, TimeoutError):
        return ModbusTimeoutError(str(err))
    return ModbusConnectionError(error_message)


async def _open(
    make_client: Callable[[Callable[[bool], None]], ModbusBaseClient],
    message_spacing: float,
    error_message: str,
) -> PymodbusConnection:
    """Construct, connect, and wrap a pymodbus client.

    Maps every backend failure — a raising constructor, a raising ``connect()``,
    or a falsy ``connect()`` — onto a neutral exception so callers never see a
    raw pymodbus type: a bad-configuration ``ParameterException`` becomes
    ``ValueError``, every other failure ``ModbusConnectionError``.
    ``make_client`` receives the connection's trace-connect hook and returns the
    not-yet-connected client.
    """
    connection = PymodbusConnection.__new__(PymodbusConnection)
    try:
        client = make_client(connection._on_trace_connect)
    except (ModbusException, OSError) as err:
        raise _connect_error(err, error_message) from err
    PymodbusConnection.__init__(connection, client, message_spacing)
    try:
        connected = await client.connect()
    except (ModbusException, OSError) as err:
        _safe_close(client)
        raise _connect_error(err, error_message) from err
    if not connected or not client.connected:
        _safe_close(client)
        raise ModbusConnectionError(error_message)
    return connection


class _GenericDiagnostic(DiagnosticBase):
    """A diagnostics request (FC 0x08) with a caller-supplied sub-function.

    pymodbus only ships fixed-sub-function diagnostic PDUs; this lets us issue an
    arbitrary sub-function as the spec's generic ``diagnostics()`` requires.
    """

    sub_function_code = 0


def _build_diagnostic(sub_function: int, data: int) -> DiagnosticBase:
    request = _GenericDiagnostic(message=data)
    request.sub_function_code = sub_function
    return request


def _socket_framer(framer: SocketFraming) -> FramerType:
    """Map a TCP/UDP framing name onto pymodbus's FramerType, or raise."""
    if framer == "socket":
        return FramerType.SOCKET
    if framer == "rtu":
        return FramerType.RTU
    if framer == "ascii":
        return FramerType.ASCII
    raise ValueError(f"unknown framer {framer!r}; expected 'socket', 'rtu', or 'ascii'")


def _serial_framer(framer: SerialFraming) -> FramerType:
    """Map a serial framing name onto pymodbus's FramerType, or raise."""
    if framer == "rtu":
        return FramerType.RTU
    if framer == "ascii":
        return FramerType.ASCII
    raise ValueError(f"unknown serial framer {framer!r}; expected 'rtu' or 'ascii'")


class PymodbusConnection:
    """A live pymodbus connection.

    Created by ``connect_tcp`` / ``connect_serial``; never instantiated directly
    by consumers. Owns the ``close()`` lifecycle. Request serialization is
    pymodbus's job: its transaction manager already holds a per-client lock for
    the full request/response cycle, so this wrapper adds none of its own.

    Inter-request spacing is the exception: pymodbus has no native
    ``wait_between_requests`` (tmodbus does), so when ``message_spacing`` is set
    ``_paced`` reproduces it here, mirroring how tmodbus enforces it internally —
    a lock makes the wait atomic across every unit on the link, and the gap is
    measured from each request finishing.
    """

    def __init__(self, client: ModbusBaseClient, message_spacing: float = 0.0) -> None:
        if message_spacing < 0:
            raise ValueError("message_spacing must be non-negative")
        self._client = client
        self._message_spacing = message_spacing
        self._request_lock = asyncio.Lock()
        self._last_request_finished_at = 0.0
        self._lost_callbacks: list[Callable[[], None]] = []

    @asynccontextmanager
    async def _paced(self) -> AsyncIterator[None]:
        """Hold each request until ``message_spacing`` has elapsed since the last
        one finished, serializing so the gap holds across every unit on the link.

        No-op when spacing is disabled (``0``).
        """
        if not self._message_spacing:
            yield
            return
        async with self._request_lock:
            elapsed = time.monotonic() - self._last_request_finished_at
            wait = self._message_spacing - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                yield
            finally:
                self._last_request_finished_at = time.monotonic()

    # -- spec surface ---------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._client.connected

    def for_unit(self, unit_id: int) -> PymodbusUnit:
        return PymodbusUnit(self, unit_id)

    def on_connection_lost(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._lost_callbacks.append(callback)

        def unsubscribe() -> None:
            try:
                self._lost_callbacks.remove(callback)
            except ValueError:
                pass

        return unsubscribe

    async def close(self) -> None:
        try:
            self._client.close()
        except (ModbusException, OSError) as err:
            raise ModbusConnectionError(str(err)) from err

    # -- internals ------------------------------------------------------------

    def _on_trace_connect(self, connecting: bool) -> None:
        """pymodbus trace hook: called True on connect, False on disconnect."""
        if not connecting:
            for callback in list(self._lost_callbacks):
                callback()


class PymodbusUnit:
    """A stateless per-unit handle. Every method raises on failure."""

    def __init__(self, connection: PymodbusConnection, unit_id: int) -> None:
        self._conn = connection
        self._client = connection._client
        self._unit_id = unit_id

    @property
    def connected(self) -> bool:
        return self._conn.connected

    # -- raw register I/O -----------------------------------------------------

    @_map_errors
    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        response = _check(
            await self._client.read_holding_registers(
                address, count=count, device_id=self._unit_id
            )
        )
        return response.registers

    @_map_errors
    async def read_input_registers(self, address: int, count: int) -> list[int]:
        response = _check(
            await self._client.read_input_registers(
                address, count=count, device_id=self._unit_id
            )
        )
        return response.registers

    @_map_errors
    async def write_register(self, address: int, value: int) -> None:
        _check(
            await self._client.write_register(address, value, device_id=self._unit_id)
        )

    @_map_errors
    async def write_registers(self, address: int, values: list[int]) -> None:
        _check(
            await self._client.write_registers(address, values, device_id=self._unit_id)
        )

    # -- raw coil / discrete-input I/O ----------------------------------------

    @_map_errors
    async def read_coils(self, address: int, count: int) -> list[bool]:
        response = _check(
            await self._client.read_coils(address, count=count, device_id=self._unit_id)
        )
        return response.bits[:count]

    @_map_errors
    async def read_discrete_inputs(self, address: int, count: int) -> list[bool]:
        response = _check(
            await self._client.read_discrete_inputs(
                address, count=count, device_id=self._unit_id
            )
        )
        return response.bits[:count]

    @_map_errors
    async def write_coil(self, address: int, value: bool) -> None:
        _check(await self._client.write_coil(address, value, device_id=self._unit_id))

    @_map_errors
    async def write_coils(self, address: int, values: list[bool]) -> None:
        _check(await self._client.write_coils(address, values, device_id=self._unit_id))

    # -- full function-code surface -------------------------------------------

    @_map_errors
    async def read_exception_status(self) -> int:  # 0x07
        response = _check(
            await self._client.read_exception_status(device_id=self._unit_id)
        )
        return int(response.status)

    @_map_errors
    async def report_server_id(self) -> bytes:  # 0x11
        response = _check(await self._client.report_device_id(device_id=self._unit_id))
        return bytes(response.identifier)

    @_map_errors
    async def mask_write_register(
        self, address: int, and_mask: int, or_mask: int
    ) -> None:  # 0x16
        _check(
            await self._client.mask_write_register(
                address=address,
                and_mask=and_mask,
                or_mask=or_mask,
                device_id=self._unit_id,
            )
        )

    @_map_errors
    async def read_write_registers(
        self,
        read_address: int,
        read_count: int,
        write_address: int,
        write_values: list[int],
    ) -> list[int]:  # 0x17
        response = _check(
            await self._client.readwrite_registers(
                read_address=read_address,
                read_count=read_count,
                write_address=write_address,
                values=write_values,
                device_id=self._unit_id,
            )
        )
        return response.registers

    @_map_errors
    async def read_fifo_queue(self, address: int) -> list[int]:  # 0x18
        response = _check(
            await self._client.read_fifo_queue(address=address, device_id=self._unit_id)
        )
        return response.values

    @_map_errors
    async def read_device_identification(self) -> dict[int, bytes]:  # 0x2B / 0x0E
        response = _check(
            await self._client.read_device_information(device_id=self._unit_id)
        )
        return response.information

    @_map_errors
    async def read_file_record(
        self, file: int, record: int, length: int
    ) -> list[int]:  # 0x14
        request_record = FileRecord(
            file_number=file, record_number=record, record_length=length
        )
        response = _check(
            await self._client.read_file_record(
                records=[request_record], device_id=self._unit_id
            )
        )
        data = response.records[0].record_data
        return [int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data), 2)]

    @_map_errors
    async def write_file_record(
        self, file: int, record: int, values: list[int]
    ) -> None:  # 0x15
        payload = b"".join(value.to_bytes(2, "big") for value in values)
        request_record = FileRecord(
            file_number=file,
            record_number=record,
            record_length=len(values),
            record_data=payload,
        )
        _check(
            await self._client.write_file_record(
                records=[request_record], device_id=self._unit_id
            )
        )

    @_map_errors
    async def diagnostics(self, sub_function: int, data: int = 0) -> int:  # 0x08
        request = _build_diagnostic(sub_function, data)
        request.dev_id = self._unit_id
        response = _check(await self._client.execute(False, request))
        message = response.message
        if isinstance(message, (bytes, bytearray)):
            return int.from_bytes(message, "big")
        if isinstance(message, (list, tuple)):
            return int(message[0]) if message else 0
        return int(message)

    @_map_errors
    async def get_comm_event_counter(self) -> tuple[int, int]:  # 0x0B
        response = _check(
            await self._client.diag_get_comm_event_counter(device_id=self._unit_id)
        )
        return int(response.status), int(response.count)

    @_map_errors
    async def get_comm_event_log(self) -> bytes:  # 0x0C
        response = _check(
            await self._client.diag_get_comm_event_log(device_id=self._unit_id)
        )
        return b"".join(int(event).to_bytes(1, "big") for event in response.events)

    def on_connection_lost(self, callback: Callable[[], None]) -> Callable[[], None]:
        return self._conn.on_connection_lost(callback)


async def connect_tcp(
    host: str,
    *,
    port: int = 502,
    timeout: float = 3,
    name: str = "modbus_connection",
    framer: SocketFraming = "socket",
    message_spacing: float = 0.0,
) -> PymodbusConnection:
    """Open a Modbus TCP / RTU-over-TCP / ASCII-over-TCP connection.

    ``framer`` selects the wire framing: ``"socket"`` for native Modbus TCP
    (MBAP), ``"rtu"`` for RTU-over-TCP — what transparent serial-to-Ethernet
    gateways speak (the bytes on the wire are plain Modbus RTU frames) — or
    ``"ascii"`` for ASCII frames tunnelled over the TCP stream.

    ``message_spacing`` is the minimum interval, in seconds, between consecutive
    requests on this connection — applied across every unit sharing the link. Use
    it for devices that need a pause between frames; ``0`` (the default) disables
    pacing and leaves serialization entirely to pymodbus.

    Raises ``ModbusConnectionError`` if the connection cannot be established. The
    connection does not self-reconnect (``reconnect_delay=0``): on loss the owner
    recreates it.
    """
    framer_type = _socket_framer(framer)
    return await _open(
        lambda trace: AsyncModbusTcpClient(
            host,
            port=port,
            timeout=timeout,
            name=name,
            reconnect_delay=0,
            framer=framer_type,
            trace_connect=trace,
        ),
        message_spacing,
        f"could not connect to {host}:{port}",
    )


async def connect_udp(
    host: str,
    *,
    port: int = 502,
    timeout: float = 3,
    name: str = "modbus_connection",
    framer: SocketFraming = "socket",
    message_spacing: float = 0.0,
) -> PymodbusConnection:
    """Open a Modbus UDP connection and return a live handle.

    UDP carries the same wire framing as TCP — ``framer`` selects ``"socket"``
    for native Modbus (MBAP), ``"rtu"`` for RTU framing, or ``"ascii"`` for ASCII
    framing over UDP. UDP is connectionless, so ``connect()`` only binds the
    local datagram endpoint; a dead peer surfaces as a timeout on the first
    request.

    ``message_spacing`` is the minimum interval, in seconds, between consecutive
    requests on this connection (see ``connect_tcp``); ``0`` (the default)
    disables pacing.

    Raises ``ModbusConnectionError`` if the endpoint cannot be set up. The
    connection does not self-reconnect (``reconnect_delay=0``).
    """
    framer_type = _socket_framer(framer)
    return await _open(
        lambda trace: AsyncModbusUdpClient(
            host,
            port=port,
            timeout=timeout,
            name=name,
            reconnect_delay=0,
            framer=framer_type,
            trace_connect=trace,
        ),
        message_spacing,
        f"could not connect to {host}:{port}",
    )


async def connect_tls(
    host: str,
    *,
    port: int = 802,
    sslctx: ssl.SSLContext | None = None,
    certfile: str | None = None,
    keyfile: str | None = None,
    password: str | None = None,
    timeout: float = 3,
    name: str = "modbus_connection",
    message_spacing: float = 0.0,
) -> PymodbusConnection:
    """Open a Modbus/TLS (Modbus Security) connection and return a live handle.

    The wire framing is always TLS. Pass a fully-configured ``sslctx`` to control
    server verification and trust; otherwise one is built from the optional
    client ``certfile`` / ``keyfile`` / ``password`` (``sslctx`` takes precedence
    over those). The generated context does **not** verify the server
    certificate — supply your own ``sslctx`` to require verification.

    ``message_spacing`` is the minimum interval, in seconds, between consecutive
    requests on this connection (see ``connect_tcp``); ``0`` (the default)
    disables pacing.

    Raises ``ModbusConnectionError`` if the connection cannot be established. The
    connection does not self-reconnect (``reconnect_delay=0``).
    """
    context = sslctx or AsyncModbusTlsClient.generate_ssl(
        certfile=certfile, keyfile=keyfile, password=password
    )
    return await _open(
        lambda trace: AsyncModbusTlsClient(
            host,
            sslctx=context,
            port=port,
            timeout=timeout,
            name=name,
            reconnect_delay=0,
            framer=FramerType.TLS,
            trace_connect=trace,
        ),
        message_spacing,
        f"could not connect to {host}:{port}",
    )


async def connect_serial(
    port: str,
    *,
    baudrate: int = 9600,
    bytesize: int = 8,
    parity: str = "N",
    stopbits: int = 1,
    timeout: float = 3,
    name: str = "modbus_connection",
    framer: SerialFraming = "rtu",
    message_spacing: float = 0.0,
) -> PymodbusConnection:
    """Open a Modbus serial connection and return a live handle.

    ``framer`` selects the serial framing: ``"rtu"`` for binary Modbus RTU
    (the default) or ``"ascii"`` for the ASCII transmission mode.

    ``message_spacing`` is the minimum interval, in seconds, between consecutive
    requests on this connection (see ``connect_tcp``); ``0`` (the default)
    disables pacing.

    Raises ``ModbusConnectionError`` if the port cannot be opened. The connection
    does not self-reconnect (``reconnect_delay=0``).
    """
    framer_type = _serial_framer(framer)
    return await _open(
        lambda trace: AsyncModbusSerialClient(
            port,
            framer=framer_type,
            baudrate=baudrate,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            timeout=timeout,
            name=name,
            reconnect_delay=0,
            trace_connect=trace,
        ),
        message_spacing,
        f"could not open serial port {port}",
    )
