"""Shared types for the modbus_connection abstraction."""

from typing import Literal

WordOrder = Literal["big", "little"]
"""Order of 16-bit registers within a multi-register value.

``"big"`` puts the most-significant word first (the common Modbus convention);
``"little"`` puts the least-significant word first. Byte order *within* each
register is always big-endian, per the Modbus spec.
"""

WriteMode = Literal["auto", "single", "multiple"]
"""How a writable register field is written to the wire.

``"auto"`` (the default) picks the function code by payload width: FC06
(write-single-register) for a one-word value, FC16 (write-multiple-registers)
otherwise. Some devices contradict that heuristic, so override it per field:
``"single"`` always uses FC06 (for a device that rejects multi-register writes;
only valid for a one-word value), ``"multiple"`` always uses FC16 (for a device
that honours only FC16, even for a single register). Updating only some bits of a
register is selected separately, by giving the field a ``write_mask``; the mode
then picks the function code of the write-back leg.
"""
