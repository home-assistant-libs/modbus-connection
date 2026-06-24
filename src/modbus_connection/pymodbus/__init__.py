"""pymodbus-backed implementation of the modbus_connection Protocols.

Provides the two connect functions (``connect_tcp`` / ``connect_serial``) plus
the concrete ``PymodbusConnection`` / ``PymodbusUnit`` classes. These are the
only backend-specific touchpoints — swapping to tmodbus changes only the import.

Requires the ``[pymodbus]`` extra.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Literal

from pymodbus import FramerType
from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient
from pymodbus.client.base import ModbusBaseClient
from pymodbus.client.mixin import ModbusClientMixin
from pymodbus.exceptions import (
    ConnectionException,
    ModbusException,
    ModbusIOException,
)
from pymodbus.pdu import ExceptionResponse, ModbusPDU
from pymodbus.pdu.diag_message import DiagnosticBase
from pymodbus.pdu.file_message import FileRecord

from .._types import WordOrder
from ..exceptions import (
    ModbusConnectionError,
    ModbusError,
    ModbusExceptionError,
    ModbusTimeoutError,
)

DATATYPE = ModbusClientMixin.DATATYPE

Framing = Literal["socket", "rtu"]

__all__ = [
    "PymodbusConnection",
    "PymodbusUnit",
    "connect_serial",
    "connect_tcp",
]


def _map_errors[**P, R](
    func: Callable[P, Awaitable[R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    """Map pymodbus transport exceptions onto the neutral hierarchy."""

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await func(*args, **kwargs)
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


class PymodbusConnection:
    """A live pymodbus connection.

    Created by ``connect_tcp`` / ``connect_serial``; never instantiated directly
    by consumers. Owns the ``close()`` lifecycle. Request serialization is
    pymodbus's job: its transaction manager already holds a per-client lock for
    the full request/response cycle, so this wrapper adds none of its own.
    """

    def __init__(self, client: ModbusBaseClient) -> None:
        self._client = client
        self._lost_callbacks: list[Callable[[], None]] = []

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
        self._client.close()

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

    # -- typed reads / writes -------------------------------------------------

    async def read_uint16(self, address: int) -> int:
        registers = await self.read_holding_registers(address, 1)
        return int(ModbusClientMixin.convert_from_registers(registers, DATATYPE.UINT16))

    async def read_int16(self, address: int) -> int:
        registers = await self.read_holding_registers(address, 1)
        return int(ModbusClientMixin.convert_from_registers(registers, DATATYPE.INT16))

    async def read_uint32(self, address: int, *, word_order: WordOrder = "big") -> int:
        registers = await self.read_holding_registers(address, 2)
        return int(
            ModbusClientMixin.convert_from_registers(
                registers, DATATYPE.UINT32, word_order=word_order
            )
        )

    async def read_float32(
        self, address: int, *, word_order: WordOrder = "big"
    ) -> float:
        registers = await self.read_holding_registers(address, 2)
        return float(
            ModbusClientMixin.convert_from_registers(
                registers, DATATYPE.FLOAT32, word_order=word_order
            )
        )

    async def read_string(self, address: int, length: int) -> str:
        registers = await self.read_holding_registers(address, length)
        value = ModbusClientMixin.convert_from_registers(registers, DATATYPE.STRING)
        return str(value).rstrip("\x00")

    async def write_uint16(self, address: int, value: int) -> None:
        registers = ModbusClientMixin.convert_to_registers(value, DATATYPE.UINT16)
        await self.write_registers(address, registers)

    async def write_float32(
        self, address: int, value: float, *, word_order: WordOrder = "big"
    ) -> None:
        registers = ModbusClientMixin.convert_to_registers(
            value, DATATYPE.FLOAT32, word_order=word_order
        )
        await self.write_registers(address, registers)

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
    framer: Framing = "socket",
) -> PymodbusConnection:
    """Open a Modbus TCP / RTU-over-TCP connection and return a live handle.

    ``framer`` selects the wire framing: ``"socket"`` for native Modbus TCP
    (MBAP), or ``"rtu"`` for RTU-over-TCP — what transparent serial-to-Ethernet
    gateways speak (the bytes on the wire are plain Modbus RTU frames).

    Raises ``ModbusConnectionError`` if the connection cannot be established. The
    connection does not self-reconnect (``reconnect_delay=0``): on loss the owner
    recreates it.
    """
    connection = PymodbusConnection.__new__(PymodbusConnection)
    client = AsyncModbusTcpClient(
        host,
        port=port,
        timeout=timeout,
        name=name,
        reconnect_delay=0,
        framer=FramerType.RTU if framer == "rtu" else FramerType.SOCKET,
        trace_connect=connection._on_trace_connect,
    )
    PymodbusConnection.__init__(connection, client)
    if not await client.connect() or not client.connected:
        client.close()
        raise ModbusConnectionError(f"could not connect to {host}:{port}")
    return connection


async def connect_serial(
    port: str,
    *,
    baudrate: int = 9600,
    bytesize: int = 8,
    parity: str = "N",
    stopbits: int = 1,
    timeout: float = 3,
    name: str = "modbus_connection",
) -> PymodbusConnection:
    """Open a Modbus serial (RTU) connection and return a live handle.

    Raises ``ModbusConnectionError`` if the port cannot be opened. The connection
    does not self-reconnect (``reconnect_delay=0``).
    """
    connection = PymodbusConnection.__new__(PymodbusConnection)
    client = AsyncModbusSerialClient(
        port,
        baudrate=baudrate,
        bytesize=bytesize,
        parity=parity,
        stopbits=stopbits,
        timeout=timeout,
        name=name,
        reconnect_delay=0,
        trace_connect=connection._on_trace_connect,
    )
    PymodbusConnection.__init__(connection, client)
    if not await client.connect() or not client.connected:
        client.close()
        raise ModbusConnectionError(f"could not open serial port {port}")
    return connection
