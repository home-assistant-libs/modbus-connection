"""modbus_connection — a small, backend-neutral Modbus connection abstraction.

The top-level package is the pure interface: the ``ModbusConnection`` /
``ModbusUnit`` Protocols, the shared ``WordOrder`` type, and the exception
hierarchy. It imports no Modbus backend and no Home Assistant.

Pick a backend to actually talk to a device:

- ``modbus_connection.pymodbus`` — ``connect_tcp`` / ``connect_serial`` over
  pymodbus (install the ``[pymodbus]`` extra).
- ``modbus_connection.tmodbus`` — the same over tmodbus (the ``[tmodbus]`` extra).
"""

from ._protocol import ModbusConnection, ModbusUnit
from ._types import WordOrder
from .exceptions import (
    ModbusConnectionError,
    ModbusError,
    ModbusExceptionError,
    ModbusTimeoutError,
)

__all__ = [
    "ModbusConnection",
    "ModbusConnectionError",
    "ModbusError",
    "ModbusExceptionError",
    "ModbusTimeoutError",
    "ModbusUnit",
    "WordOrder",
]
