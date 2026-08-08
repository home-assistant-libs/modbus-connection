"""Provide backend-neutral Modbus connections, units, and shared types."""

from ._client import (
    ModbusSerialParams,
    ModbusTcpParams,
    ModbusTlsParams,
    ModbusUdpParams,
)
from ._protocol import ModbusConnection, ModbusUnit
from ._types import WordOrder
from .exceptions import (
    BlockReadError,
    ClientClosedError,
    ModbusConnectionError,
    ModbusError,
    ModbusExceptionError,
    ModbusProtocolError,
    ModbusTimeoutError,
)

__all__ = [
    "BlockReadError",
    "ClientClosedError",
    "ModbusConnection",
    "ModbusConnectionError",
    "ModbusError",
    "ModbusExceptionError",
    "ModbusProtocolError",
    "ModbusSerialParams",
    "ModbusTcpParams",
    "ModbusTimeoutError",
    "ModbusTlsParams",
    "ModbusUdpParams",
    "ModbusUnit",
    "WordOrder",
]
