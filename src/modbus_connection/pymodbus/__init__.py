"""pymodbus-backed implementation of the modbus_connection abstraction.

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
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Concatenate

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

from .._client import (
    BaseModbusConnection,
    ModbusParams,
    ModbusSerialParams,
    ModbusTcpParams,
    ModbusTlsParams,
    ModbusUdpParams,
)
from .._tls import build_tls_context
from .._types import SerialFraming, SocketFraming
from ..exceptions import (
    ModbusConnectionError,
    ModbusError,
    ModbusExceptionError,
    ModbusTimeoutError,
)

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
            async with self._conn._pacer.paced(self._unit_id):
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


def _connect_error(
    err: Exception | None, params: ModbusParams, target: str
) -> Exception:
    """Translate a pymodbus construct/connect failure to the neutral type.

    Builds its own "could not …" message from ``params`` (serial vs not) and
    ``target``. A ``ParameterException`` means the caller passed bad
    configuration, not that the link is down — surface it as ``ValueError`` (as
    the framer mappers do) instead of masking a caller bug as a transient
    connection failure. A ``TimeoutError`` (the connect attempt did not complete
    in time) stays a timeout, mirroring the operational path. Every other
    transport failure — and a client that reported not-connected, passed as
    ``err=None`` — becomes ``ModbusConnectionError``.
    """
    if isinstance(err, ParameterException):
        return ValueError(str(err))
    if isinstance(err, TimeoutError):
        return ModbusTimeoutError(str(err))
    message = (
        f"could not open serial port {target}"
        if isinstance(params, ModbusSerialParams)
        else f"could not connect to {target}"
    )
    return ModbusConnectionError(message)


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


# Framing name -> pymodbus FramerType. Serial links accept only the rtu/ascii
# subset (see the serial branch of ``_create_client``); the socket transports
# take any of the three.
_FRAMER_TYPES: dict[str, FramerType] = {
    "socket": FramerType.SOCKET,
    "rtu": FramerType.RTU,
    "ascii": FramerType.ASCII,
}


def _framer_type(framer: SocketFraming) -> FramerType:
    """Map a TCP/UDP framing name onto pymodbus's ``FramerType``; raise
    ``ValueError`` on an unknown name."""
    try:
        return _FRAMER_TYPES[framer]
    except KeyError:
        raise ValueError(
            f"unknown framer {framer!r}; expected 'socket', 'rtu', or 'ascii'"
        ) from None


class PymodbusConnection(BaseModbusConnection):
    """A Modbus connection backed by pymodbus."""

    def __init__(
        self,
        params: ModbusParams,
        *,
        timeout: float = 3,
        message_spacing: float = 0.0,
    ) -> None:
        super().__init__(params, timeout=timeout, message_spacing=message_spacing)
        # A caller-supplied TLS context, set by connect_tls; overrides the
        # context built from the params.
        self._sslctx: ssl.SSLContext | None = None

    # -- spec surface ---------------------------------------------------------

    def for_unit(self, unit_id: int) -> PymodbusUnit:
        return PymodbusUnit(self, unit_id)

    async def close(self) -> None:
        if self._client is None:
            return
        try:
            self._client.close()
        except (ModbusException, OSError) as err:
            raise ModbusConnectionError(str(err)) from err

    # -- internals ------------------------------------------------------------

    async def _async_create_client(self) -> ModbusBaseClient:
        # Unlike tmodbus's create_* functions, pymodbus client constructors can
        # raise; map those failures like any other connect failure.
        try:
            return await self._create_client()
        except (ModbusException, OSError) as err:
            raise _connect_error(err, self._params, self._target) from err

    async def _create_client(self) -> ModbusBaseClient:
        params = self._params
        if isinstance(params, ModbusTcpParams):
            return AsyncModbusTcpClient(
                params.host,
                port=params.port,
                timeout=self._timeout,
                name="modbus_connection",
                reconnect_delay=0,
                framer=_framer_type(params.framer),
                trace_connect=self._on_trace_connect,
            )
        if isinstance(params, ModbusUdpParams):
            return AsyncModbusUdpClient(
                params.host,
                port=params.port,
                timeout=self._timeout,
                name="modbus_connection",
                reconnect_delay=0,
                framer=_framer_type(params.framer),
                trace_connect=self._on_trace_connect,
            )
        if isinstance(params, ModbusTlsParams):
            context = (
                self._sslctx
                if self._sslctx is not None
                else await asyncio.to_thread(
                    build_tls_context,
                    params.verify,
                    params.check_hostname,
                    params.client_cert,
                    params.client_key,
                    params.client_key_password,
                )
            )
            return AsyncModbusTlsClient(
                params.host,
                sslctx=context,
                port=params.port,
                timeout=self._timeout,
                name="modbus_connection",
                reconnect_delay=0,
                framer=FramerType.TLS,
                trace_connect=self._on_trace_connect,
            )
        if params.framer not in ("rtu", "ascii"):
            raise ValueError(
                f"unknown serial framer {params.framer!r}; expected 'rtu' or 'ascii'"
            )
        return AsyncModbusSerialClient(
            params.device,
            framer=_FRAMER_TYPES[params.framer],
            baudrate=params.baudrate,
            bytesize=params.bytesize,
            parity=params.parity,
            stopbits=params.stopbits,
            timeout=self._timeout,
            name="modbus_connection",
            reconnect_delay=0,
            trace_connect=self._on_trace_connect,
        )

    async def _connect_client(self) -> None:
        try:
            connected = await self._client.connect()
        except (ModbusException, OSError) as err:
            _safe_close(self._client)
            raise _connect_error(err, self._params, self._target) from err
        if not connected or not self._client.connected:
            _safe_close(self._client)
            raise _connect_error(None, self._params, self._target)

    def _on_trace_connect(self, connecting: bool) -> None:
        """pymodbus trace hook: called True on connect, False on disconnect."""
        if not connecting:
            self._lost_callbacks.fire()


class PymodbusUnit:
    """A stateless per-unit handle. Every method raises on failure.

    The backend client is resolved through the owning connection on use, so
    handles can be handed out before the connection is established; a request
    on an unestablished connection raises ``ModbusConnectionError``.
    """

    def __init__(self, connection: PymodbusConnection, unit_id: int) -> None:
        self._conn = connection
        self._unit_id = unit_id

    @property
    def _client(self) -> ModbusBaseClient:
        client = self._conn._client
        if client is None:
            raise ModbusConnectionError("connection is not established")
        return client

    @property
    def connected(self) -> bool:
        return self._conn.connected

    def set_message_spacing(self, seconds: float) -> None:
        self._conn._pacer.set_unit_spacing(self._unit_id, seconds)

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
        # pymodbus types every response as the base ModbusPDU; the concrete
        # response subclass carries the function-code-specific attribute.
        return bytes(response.identifier)  # type: ignore[attr-defined]

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
        return response.values  # type: ignore[attr-defined]  # concrete response attr

    @_map_errors
    async def read_device_identification(self) -> dict[int, bytes]:  # 0x2B / 0x0E
        response = _check(
            await self._client.read_device_information(device_id=self._unit_id)
        )
        return response.information  # type: ignore[attr-defined]  # concrete response attr

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
        data = response.records[0].record_data  # type: ignore[attr-defined]  # concrete response attr
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
        message = response.message  # type: ignore[attr-defined]  # concrete response attr
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
        return b"".join(
            int(event).to_bytes(1, "big")
            for event in response.events  # type: ignore[attr-defined]  # concrete response attr
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

    Raises ``ModbusConnectionError`` if the connection cannot be established.
    """
    connection = PymodbusConnection(
        ModbusTcpParams(host=host, port=port, framer=framer),
        timeout=timeout,
        message_spacing=message_spacing,
    )
    await connection.connect()
    return connection


async def connect_udp(
    host: str,
    *,
    port: int = 502,
    timeout: float = 3,
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

    Raises ``ModbusConnectionError`` if the endpoint cannot be set up.
    """
    connection = PymodbusConnection(
        ModbusUdpParams(host=host, port=port, framer=framer),
        timeout=timeout,
        message_spacing=message_spacing,
    )
    await connection.connect()
    return connection


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
) -> PymodbusConnection:
    """Open a Modbus/TLS (Modbus Security) connection and return a live handle.

    The wire framing is always TLS. Two groups of arguments split the *server*
    side from the *client* side.

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

    ``message_spacing`` is the minimum interval, in seconds, between consecutive
    requests on this connection (see ``connect_tcp``); ``0`` (the default)
    disables pacing.

    Raises ``ModbusConnectionError`` if the connection cannot be established.
    """
    connection = PymodbusConnection(
        ModbusTlsParams(
            host=host,
            port=port,
            verify=verify,
            check_hostname=check_hostname,
            client_cert=client_cert,
            client_key=client_key,
            client_key_password=client_key_password,
        ),
        timeout=timeout,
        message_spacing=message_spacing,
    )
    connection._sslctx = sslctx
    await connection.connect()
    return connection


async def connect_serial(
    port: str,
    *,
    baudrate: int = 9600,
    bytesize: int = 8,
    parity: str = "N",
    stopbits: int = 1,
    timeout: float = 3,
    framer: SerialFraming = "rtu",
    message_spacing: float = 0.0,
) -> PymodbusConnection:
    """Open a Modbus serial connection and return a live handle.

    ``framer`` selects the serial framing: ``"rtu"`` for binary Modbus RTU
    (the default) or ``"ascii"`` for the ASCII transmission mode.

    ``message_spacing`` is the minimum interval, in seconds, between consecutive
    requests on this connection (see ``connect_tcp``); ``0`` (the default)
    disables pacing.

    Raises ``ModbusConnectionError`` if the port cannot be opened.
    """
    connection = PymodbusConnection(
        ModbusSerialParams(
            device=port,
            baudrate=baudrate,
            bytesize=bytesize,  # type: ignore[arg-type]
            parity=parity,  # type: ignore[arg-type]
            stopbits=stopbits,  # type: ignore[arg-type]
            framer=framer,
        ),
        timeout=timeout,
        message_spacing=message_spacing,
    )
    await connection.connect()
    return connection
