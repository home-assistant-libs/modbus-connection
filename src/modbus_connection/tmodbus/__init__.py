"""tmodbus-backed implementation of the modbus_connection Protocols.

Mirrors the pymodbus backend over tmodbus. Per the design, three function codes
have no tmodbus equivalent and raise ``NotImplementedError``: diagnostics (0x08),
get-comm-event-counter (0x0B), and get-comm-event-log (0x0C). File records
(0x14/0x15) are issued through tmodbus's ``execute(pdu)`` seam.

Requires the ``[tmodbus]`` extra.
"""

from __future__ import annotations

import functools
import struct
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Concatenate, Literal

from tmodbus import (
    AsyncModbusClient,
    create_async_rtu_client,
    create_async_rtu_over_tcp_client,
    create_async_tcp_client,
)
from tmodbus.exceptions import (
    ModbusConnectionError as TModbusConnectionError,
)
from tmodbus.exceptions import (
    ModbusResponseError,
    RequestRetryFailedError,
    TModbusError,
)
from tmodbus.pdu import (
    FileRecord,
    FileRecordRequest,
    ReadFileRecordPDU,
    WriteFileRecordPDU,
)

from .._types import WordOrder
from ..exceptions import (
    ModbusConnectionError,
    ModbusError,
    ModbusExceptionError,
    ModbusTimeoutError,
)

Framing = Literal["socket", "rtu"]

__all__ = [
    "TmodbusConnection",
    "TmodbusUnit",
    "connect_serial",
    "connect_tcp",
]


def _registers_to_value(
    registers: list[int], fmt: str, word_order: WordOrder
) -> object:
    ordered = list(reversed(registers)) if word_order == "little" else registers
    raw = b"".join(int(reg).to_bytes(2, "big") for reg in ordered)
    return struct.unpack(">" + fmt, raw)[0]


def _value_to_registers(value: object, fmt: str, word_order: WordOrder) -> list[int]:
    raw = struct.pack(">" + fmt, value)
    registers = [int.from_bytes(raw[i : i + 2], "big") for i in range(0, len(raw), 2)]
    return list(reversed(registers)) if word_order == "little" else registers


class TmodbusConnection:
    """A live tmodbus connection."""

    def __init__(self, client: AsyncModbusClient) -> None:
        self._client = client
        self._lost_callbacks: list[Callable[[], None]] = []

    @property
    def connected(self) -> bool:
        return self._client.connected

    def for_unit(self, unit_id: int) -> TmodbusUnit:
        return TmodbusUnit(self, self._client.for_unit_id(unit_id))

    def on_connection_lost(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._lost_callbacks.append(callback)

        def unsubscribe() -> None:
            try:
                self._lost_callbacks.remove(callback)
            except ValueError:
                pass

        return unsubscribe

    async def close(self) -> None:
        await self._client.disconnect()

    def _notify_lost(self) -> None:
        for callback in list(self._lost_callbacks):
            callback()


def _map_errors[**P, R](
    func: Callable[Concatenate[TmodbusUnit, P], Awaitable[R]],
) -> Callable[Concatenate[TmodbusUnit, P], Coroutine[Any, Any, R]]:
    """Map tmodbus exceptions onto the neutral hierarchy.

    Decorates ``TmodbusUnit`` methods so each body just calls the client
    directly; a connection-lost error also fires the owner's lost callbacks.
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

    # -- typed reads / writes -------------------------------------------------

    @_map_errors
    async def read_uint16(self, address: int) -> int:
        return int(await self._client.read_uint16(address))

    @_map_errors
    async def read_int16(self, address: int) -> int:
        return int(await self._client.read_int16(address))

    @_map_errors
    async def read_uint32(self, address: int, *, word_order: WordOrder = "big") -> int:
        if word_order == "big":
            return int(await self._client.read_uint32(address))
        registers = await self.read_holding_registers(address, 2)
        return int(_registers_to_value(registers, "I", word_order))

    @_map_errors
    async def read_float32(
        self, address: int, *, word_order: WordOrder = "big"
    ) -> float:
        if word_order == "big":
            return float(await self._client.read_float(address))
        registers = await self.read_holding_registers(address, 2)
        return float(_registers_to_value(registers, "f", word_order))

    @_map_errors
    async def read_string(self, address: int, length: int) -> str:
        value = await self._client.read_string(address, number_of_registers=length)
        return value.rstrip("\x00")

    @_map_errors
    async def write_uint16(self, address: int, value: int) -> None:
        await self._client.write_uint16(address, value)

    @_map_errors
    async def write_float32(
        self, address: int, value: float, *, word_order: WordOrder = "big"
    ) -> None:
        if word_order == "big":
            await self._client.write_float(address, value)
            return
        await self.write_registers(address, _value_to_registers(value, "f", word_order))

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
        pdu = ReadFileRecordPDU([FileRecordRequest(file, record, length)])
        # ReadFileRecordPDU decodes to list[bytes], one entry per requested record.
        records = await self._client.execute(pdu)
        data = records[0]
        return [int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data), 2)]

    @_map_errors
    async def write_file_record(
        self, file: int, record: int, values: list[int]
    ) -> None:  # 0x15
        payload = b"".join(int(value).to_bytes(2, "big") for value in values)
        pdu = WriteFileRecordPDU([FileRecord(file, record, payload)])
        await self._client.execute(pdu)

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
    unit_id: int = 1,
    framer: Framing = "socket",
) -> TmodbusConnection:
    """Open a Modbus TCP / RTU-over-TCP connection over tmodbus.

    ``framer`` selects the wire framing: ``"socket"`` for native Modbus TCP
    (MBAP), or ``"rtu"`` for RTU-over-TCP — what transparent serial-to-Ethernet
    gateways speak.

    ``auto_reconnect`` is disabled: on loss the owner recreates the connection.
    Raises ``ModbusConnectionError`` if the connection cannot be established.
    """
    create = (
        create_async_rtu_over_tcp_client if framer == "rtu" else create_async_tcp_client
    )
    client = create(
        host,
        port,
        unit_id=unit_id,
        timeout=timeout,
        auto_reconnect=False,
    )
    try:
        await client.connect()
    except (TimeoutError, TModbusConnectionError, OSError) as err:
        raise ModbusConnectionError(f"could not connect to {host}:{port}") from err
    return TmodbusConnection(client)


async def connect_serial(
    port: str,
    *,
    baudrate: int = 9600,
    bytesize: int = 8,
    parity: str = "N",
    stopbits: int = 1,
    unit_id: int = 1,
) -> TmodbusConnection:
    """Open a Modbus serial (RTU) connection over tmodbus and return a live handle.

    ``auto_reconnect`` is disabled. Raises ``ModbusConnectionError`` on failure.
    """
    client = create_async_rtu_client(
        port,
        unit_id=unit_id,
        baudrate=baudrate,
        bytesize=bytesize,
        parity=parity,
        stopbits=stopbits,
        auto_reconnect=False,
    )
    try:
        await client.connect()
    except (TimeoutError, TModbusConnectionError, OSError) as err:
        raise ModbusConnectionError(f"could not open serial port {port}") from err
    return TmodbusConnection(client)
