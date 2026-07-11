"""modbus_connection — a small, backend-neutral Modbus connection abstraction.

The top-level package is the pure interface: the ``ModbusConnection`` /
``ModbusUnit`` Protocols and the exception hierarchy. It also re-exports the
shared ``WordOrder`` datatype used by ``decode`` / ``encode`` / ``model`` for a
convenient public import. It imports no Modbus backend.

Pick a backend to actually talk to a device:

- ``modbus_connection.pymodbus`` — ``connect_tcp`` / ``connect_udp`` /
  ``connect_tls`` / ``connect_serial`` over pymodbus (install the ``[pymodbus]``
  extra).
- ``modbus_connection.tmodbus`` — the same over tmodbus (the ``[tmodbus]``
  extra), except UDP, which tmodbus has no transport for.
"""

from ._protocol import ModbusConnection, ModbusUnit
from ._types import WordOrder
from .exceptions import (
    BlockReadError,
    ModbusConnectionError,
    ModbusError,
    ModbusExceptionError,
    ModbusProtocolError,
    ModbusTimeoutError,
)

__all__ = [
    "BlockReadError",
    "ModbusConnection",
    "ModbusConnectionError",
    "ModbusError",
    "ModbusExceptionError",
    "ModbusProtocolError",
    "ModbusTimeoutError",
    "ModbusUnit",
    "WordOrder",
]
