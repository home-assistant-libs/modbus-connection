"""modbus_connection — a small, backend-neutral Modbus connection abstraction.

The top-level package is the pure interface: the ``ModbusConnection`` base
class and ``ModbusUnit`` Protocol to type against, the exception hierarchy, and
the shared connection-params dataclasses. It also re-exports the shared
``WordOrder`` datatype used by ``decode`` / ``encode`` / ``model`` for a
convenient public import. It imports no Modbus backend.

Pick a backend to actually talk to a device:

- ``modbus_connection.pymodbus`` — ``connect_tcp`` / ``connect_udp`` /
  ``connect_tls`` / ``connect_serial`` over pymodbus (install the ``[pymodbus]``
  extra).
- ``modbus_connection.tmodbus`` — the same over tmodbus (the ``[tmodbus]``
  extra), except UDP, which tmodbus has no transport for.
"""

from ._client import (
    BaseModbusConnection as ModbusConnection,
)
from ._client import (
    ModbusSerialParams,
    ModbusTcpParams,
    ModbusTlsParams,
    ModbusUdpParams,
)
from ._protocol import ModbusUnit
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
    "ModbusSerialParams",
    "ModbusTcpParams",
    "ModbusTimeoutError",
    "ModbusTlsParams",
    "ModbusUdpParams",
    "ModbusUnit",
    "WordOrder",
]
