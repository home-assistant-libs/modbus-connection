"""tmodbus-backed implementation of the modbus_connection abstraction."""

from __future__ import annotations

import functools
import ssl
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Concatenate

from tenacity import AsyncRetrying, retry_never, stop_after_delay, wait_exponential
from tmodbus import (
    AsyncModbusClient,
    create_async_ascii_client,
    create_async_rtu_client,
    create_async_rtu_over_tcp_client,
    create_async_tcp_client,
    create_async_udp_client,
)
from tmodbus.exceptions import (
    InvalidResponseError,
    ModbusResponseError,
    TModbusError,
)
from tmodbus.exceptions import (
    ModbusConnectionError as TModbusConnectionError,
)

from .._client import (
    BaseModbusConnection,
    ModbusParams,
    ModbusSerialParams,
    ModbusTcpParams,
    ModbusTlsParams,
    ModbusUdpParams,
)
from .._types import SerialFraming, SocketFraming
from ..exceptions import (
    ModbusConnectionError,
    ModbusError,
    ModbusExceptionError,
    ModbusProtocolError,
    ModbusTimeoutError,
)

__all__ = [
    "ModbusConnection",
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

# Repeats tmodbus's own bounds; ``reraise`` is what we are after, so an
# exhausted retry surfaces the device's busy response rather than a timeout.
_RESPONSE_RETRIES = AsyncRetrying(
    retry=retry_never,
    stop=stop_after_delay(60),
    wait=wait_exponential(min=0.1, max=10),
    reraise=True,
)


class ModbusConnection(BaseModbusConnection):
    """A Modbus connection backed by tmodbus."""

    def __init__(
        self,
        params: ModbusParams,
        *,
        timeout: float = 10,
        message_spacing: float = 0.0,
    ) -> None:
        if isinstance(params, ModbusUdpParams) and params.framer != "socket":
            raise ValueError(
                "RTU- and ASCII-over-UDP are not supported by the tmodbus "
                "ModbusConnection; use modbus_connection.pymodbus.ModbusConnection"
            )
        if isinstance(params, ModbusTcpParams) and params.framer == "ascii":
            raise ValueError(
                "ASCII-over-TCP is not supported by the tmodbus "
                "ModbusConnection; use modbus_connection.pymodbus.ModbusConnection"
            )
        super().__init__(params, timeout=timeout, message_spacing=message_spacing)
        self._unit_clients: dict[int, AsyncModbusClient] = {}

    def for_unit(self, unit_id: int) -> TmodbusUnit:
        return TmodbusUnit(self, unit_id)

    def _unit_client(self, unit_id: int) -> AsyncModbusClient:
        """The unit-bound tmodbus client for ``unit_id``."""
        if self._client is None:
            raise ModbusConnectionError("connection is not established")
        client = self._unit_clients.get(unit_id)
        if client is None:
            client = self._unit_clients[unit_id] = self._client.for_unit_id(unit_id)
        return client

    async def _close_client(self, client: AsyncModbusClient) -> None:
        self._unit_clients.clear()
        try:
            await client.disconnect()
        except (TModbusError, OSError) as err:
            raise ModbusConnectionError(str(err)) from err

    async def _connect_client(self) -> AsyncModbusClient:
        client = await self._create_client()
        error_message = (
            f"could not open serial port {self._target}"
            if isinstance(self._params, ModbusSerialParams)
            else f"could not connect to {self._target}"
        )
        try:
            await client.connect()
        except TimeoutError as err:
            raise ModbusTimeoutError(str(err)) from err
        except (TModbusError, OSError) as err:
            raise ModbusConnectionError(error_message) from err
        return client

    async def _create_client(self) -> AsyncModbusClient:
        params = self._params
        if isinstance(params, ModbusTcpParams):
            # ascii was rejected at construction.
            if params.framer == "socket":
                create = create_async_tcp_client
            else:
                create = create_async_rtu_over_tcp_client
            return create(
                params.host,
                params.port,
                unit_id=_PLACEHOLDER_UNIT_ID,
                timeout=self._timeout,
                auto_reconnect=False,
                response_retry_strategy=_RESPONSE_RETRIES,
                retry_on_device_failure=False,
                on_connection_lost=self._on_connection_lost,
            )
        if isinstance(params, ModbusUdpParams):
            # rtu and ascii framing were rejected at construction.
            return create_async_udp_client(
                params.host,
                params.port,
                unit_id=_PLACEHOLDER_UNIT_ID,
                timeout=self._timeout,
                auto_reconnect=False,
                response_retry_strategy=_RESPONSE_RETRIES,
                retry_on_device_failure=False,
                on_connection_lost=self._on_connection_lost,
            )
        if isinstance(params, ModbusTlsParams):
            return create_async_tcp_client(
                params.host,
                params.port,
                unit_id=_PLACEHOLDER_UNIT_ID,
                timeout=self._timeout,
                auto_reconnect=False,
                response_retry_strategy=_RESPONSE_RETRIES,
                retry_on_device_failure=False,
                ssl=await params.create_ssl_context(),
                on_connection_lost=self._on_connection_lost,
            )
        if params.framer == "rtu":
            create_serial = create_async_rtu_client
        else:
            create_serial = create_async_ascii_client
        # tmodbus' SerialXOptions under-declares the serial options serialx accepts
        # at runtime: it omits ``bytesize`` and types ``parity``/``stopbits`` as
        # enums though serialx also takes the str/int forms we pass here.
        return create_serial(  # type: ignore[call-arg]
            params.device,
            unit_id=_PLACEHOLDER_UNIT_ID,
            baudrate=params.baudrate,
            bytesize=params.bytesize,
            parity=params.parity,  # type: ignore[arg-type]
            stopbits=params.stopbits,  # type: ignore[arg-type]
            auto_reconnect=False,
            response_retry_strategy=_RESPONSE_RETRIES,
            retry_on_device_failure=False,
            on_connection_lost=self._on_connection_lost,
        )

    def _on_connection_lost(self, exc: Exception | None) -> None:
        # A connection is lost when the transport takes it from us; close() and
        # disconnect() are us tearing it down, and also trigger this hook. Both
        # unpublish the client before tearing down, so a hook that finds no
        # published client is observing our own teardown.
        if self._closed or self._client is None:
            return
        self._client = None
        self._unit_clients.clear()
        self._lost_callbacks.fire()


TmodbusConnection = ModbusConnection


def _map_errors[**P, R](
    func: Callable[Concatenate[TmodbusUnit, P], Awaitable[R]],
) -> Callable[Concatenate[TmodbusUnit, P], Coroutine[Any, Any, R]]:
    """Connect, pace, and map errors around a unit operation."""

    @functools.wraps(func)
    async def wrapper(self: TmodbusUnit, *args: P.args, **kwargs: P.kwargs) -> R:
        await self._conn.connect()
        try:
            async with self._conn._pacer.paced(self._unit_id):
                return await func(self, *args, **kwargs)
        except TModbusConnectionError as err:
            raise ModbusConnectionError(str(err)) from err
        except TimeoutError as err:
            raise ModbusTimeoutError(str(err)) from err
        except InvalidResponseError as err:
            raise ModbusProtocolError(str(err)) from err
        except ModbusResponseError as err:
            raise ModbusExceptionError(int(err.error_code)) from err
        except TModbusError as err:
            raise ModbusError(str(err)) from err

    return wrapper


class TmodbusUnit:
    """Represent a unit using tmodbus."""

    def __init__(self, connection: ModbusConnection, unit_id: int) -> None:
        self._conn = connection
        self._unit_id = unit_id

    @property
    def _client(self) -> AsyncModbusClient:
        return self._conn._unit_client(self._unit_id)

    @property
    def connected(self) -> bool:
        return self._conn.connected

    def set_message_spacing(self, seconds: float) -> None:
        self._conn._pacer.set_unit_spacing(self._unit_id, seconds)

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
    timeout: float = 10,
    framer: SocketFraming = "socket",
    message_spacing: float = 0.0,
) -> ModbusConnection:
    """Open a Modbus TCP connection.

    Raises ``ModbusConnectionError`` if the connection cannot be established.
    """
    connection = ModbusConnection(
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
    timeout: float = 10,
    framer: SocketFraming = "socket",
    message_spacing: float = 0.0,
) -> ModbusConnection:
    """Open a Modbus UDP connection.

    Raises ``ModbusConnectionError`` if the endpoint cannot be set up.
    """
    connection = ModbusConnection(
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
    timeout: float = 10,
    message_spacing: float = 0.0,
) -> ModbusConnection:
    """Open a Modbus/TLS connection.

    Raises ``ModbusConnectionError`` if the connection cannot be established.
    """
    connection = ModbusConnection(
        ModbusTlsParams(
            host=host,
            port=port,
            verify=verify,
            check_hostname=check_hostname,
            client_cert=client_cert,
            client_key=client_key,
            client_key_password=client_key_password,
            sslctx=sslctx,
        ),
        timeout=timeout,
        message_spacing=message_spacing,
    )
    await connection.connect()
    return connection


async def connect_serial(
    port: str,
    *,
    baudrate: int = 9600,
    bytesize: int = 8,
    parity: str = "N",
    stopbits: int = 1,
    timeout: float = 10,
    framer: SerialFraming = "rtu",
    message_spacing: float = 0.0,
) -> ModbusConnection:
    """Open a Modbus serial connection.

    Raises ``ModbusConnectionError`` on failure.
    """
    connection = ModbusConnection(
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
