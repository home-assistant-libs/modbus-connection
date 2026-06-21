"""tmodbus-backed implementation of the modbus_connection Protocols.

Mirrors the pymodbus backend over tmodbus. Per the design, three function codes
have no tmodbus equivalent and raise ``NotImplementedError``: diagnostics (0x08),
get-comm-event-counter (0x0B), and get-comm-event-log (0x0C). File records
(0x14/0x15) are issued through tmodbus's ``execute(pdu)`` seam.

Requires the ``[tmodbus]`` extra.
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Awaitable, Callable
from typing import TypeVar

from tmodbus import AsyncModbusClient, create_async_rtu_client, create_async_tcp_client
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

_T = TypeVar("_T")

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
    """A live, internally-serialized tmodbus connection."""

    def __init__(self, client: AsyncModbusClient) -> None:
        self._client = client
        self._lock = asyncio.Lock()
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

    async def _call(self, awaitable: Awaitable[_T]) -> _T:
        async with self._lock:
            try:
                return await awaitable
            except TModbusConnectionError as err:
                self._notify_lost()
                raise ModbusConnectionError(str(err)) from err
            except (TimeoutError, RequestRetryFailedError) as err:
                raise ModbusTimeoutError(str(err)) from err
            except ModbusResponseError as err:
                raise ModbusExceptionError(int(err.error_code)) from err
            except TModbusError as err:
                raise ModbusError(str(err)) from err


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

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        return list(
            await self._conn._call(self._client.read_holding_registers(address, count))
        )

    async def read_input_registers(self, address: int, count: int) -> list[int]:
        return list(
            await self._conn._call(self._client.read_input_registers(address, count))
        )

    async def write_register(self, address: int, value: int) -> None:
        await self._conn._call(self._client.write_single_register(address, value))

    async def write_registers(self, address: int, values: list[int]) -> None:
        await self._conn._call(self._client.write_multiple_registers(address, values))

    # -- raw coil / discrete-input I/O ----------------------------------------

    async def read_coils(self, address: int, count: int) -> list[bool]:
        return [
            bool(bit)
            for bit in await self._conn._call(self._client.read_coils(address, count))
        ]

    async def read_discrete_inputs(self, address: int, count: int) -> list[bool]:
        return [
            bool(bit)
            for bit in await self._conn._call(
                self._client.read_discrete_inputs(address, count)
            )
        ]

    async def write_coil(self, address: int, value: bool) -> None:
        await self._conn._call(self._client.write_single_coil(address, value))

    async def write_coils(self, address: int, values: list[bool]) -> None:
        await self._conn._call(self._client.write_multiple_coils(address, values))

    # -- typed reads / writes -------------------------------------------------

    async def read_uint16(self, address: int) -> int:
        return int(await self._conn._call(self._client.read_uint16(address)))

    async def read_int16(self, address: int) -> int:
        return int(await self._conn._call(self._client.read_int16(address)))

    async def read_uint32(self, address: int, *, word_order: WordOrder = "big") -> int:
        if word_order == "big":
            return int(await self._conn._call(self._client.read_uint32(address)))
        registers = await self.read_holding_registers(address, 2)
        return int(_registers_to_value(registers, "I", word_order))

    async def read_float32(
        self, address: int, *, word_order: WordOrder = "big"
    ) -> float:
        if word_order == "big":
            return float(await self._conn._call(self._client.read_float(address)))
        registers = await self.read_holding_registers(address, 2)
        return float(_registers_to_value(registers, "f", word_order))

    async def read_string(self, address: int, length: int) -> str:
        value = await self._conn._call(
            self._client.read_string(address, number_of_registers=length)
        )
        return str(value).rstrip("\x00")

    async def write_uint16(self, address: int, value: int) -> None:
        await self._conn._call(self._client.write_uint16(address, value))

    async def write_float32(
        self, address: int, value: float, *, word_order: WordOrder = "big"
    ) -> None:
        if word_order == "big":
            await self._conn._call(self._client.write_float(address, value))
            return
        await self.write_registers(address, _value_to_registers(value, "f", word_order))

    # -- full function-code surface -------------------------------------------

    async def read_exception_status(self) -> int:  # 0x07
        return int(await self._conn._call(self._client.read_exception_status()))

    async def report_server_id(self) -> bytes:  # 0x11
        response = await self._conn._call(self._client.read_server_id())
        return bytes(response.server_id)

    async def mask_write_register(
        self, address: int, and_mask: int, or_mask: int
    ) -> None:  # 0x16
        await self._conn._call(
            self._client.mask_write_register(address, and_mask, or_mask)
        )

    async def read_write_registers(
        self,
        read_address: int,
        read_count: int,
        write_address: int,
        write_values: list[int],
    ) -> list[int]:  # 0x17
        return list(
            await self._conn._call(
                self._client.read_write_multiple_registers(
                    read_address, read_count, write_address, write_values
                )
            )
        )

    async def read_fifo_queue(self, address: int) -> list[int]:  # 0x18
        return list(await self._conn._call(self._client.read_fifo_queue(address)))

    async def read_device_identification(self) -> dict[int, bytes]:  # 0x2B / 0x0E
        return dict(
            await self._conn._call(self._client.read_device_identification(1, 0))
        )

    async def read_file_record(
        self, file: int, record: int, length: int
    ) -> list[int]:  # 0x14
        pdu = ReadFileRecordPDU([FileRecordRequest(file, record, length)])
        response = await self._conn._call(self._client.execute(pdu))
        data = _file_record_data(response)
        return [int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data), 2)]

    async def write_file_record(
        self, file: int, record: int, values: list[int]
    ) -> None:  # 0x15
        payload = b"".join(int(value).to_bytes(2, "big") for value in values)
        pdu = WriteFileRecordPDU([FileRecord(file, record, payload)])
        await self._conn._call(self._client.execute(pdu))

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


def _file_record_data(response: object) -> bytes:
    """Extract the record bytes from a tmodbus read-file-record response."""
    records = response if isinstance(response, list) else [response]
    first = records[0]
    data = getattr(first, "data", None)
    if data is None:
        data = getattr(first, "record_data", first)
    return bytes(data)


async def connect_tcp(
    host: str,
    *,
    port: int = 502,
    timeout: float = 3,
    unit_id: int = 1,
) -> TmodbusConnection:
    """Open a Modbus TCP connection over tmodbus and return a live handle.

    ``auto_reconnect`` is disabled: on loss the owner recreates the connection.
    Raises ``ModbusConnectionError`` if the connection cannot be established.
    """
    client = create_async_tcp_client(
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
