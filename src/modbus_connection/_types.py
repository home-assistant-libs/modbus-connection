"""Shared types for the modbus_connection abstraction."""

from typing import Literal

WordOrder = Literal["big", "little"]
"""Order of 16-bit registers within a multi-register value.

``"big"`` puts the most-significant word first (the common Modbus convention);
``"little"`` puts the least-significant word first.
"""

ByteOrder = Literal["big", "little"]
"""Order of the two bytes *within* each 16-bit register.

``"big"`` keeps the most-significant byte first (the Modbus convention, and the
default everywhere); ``"little"`` swaps the bytes within each register. Combined
with :data:`WordOrder` this spells out all four byte arrangements real devices
use — ABCD (big/big), CDAB (little/big), BADC (big/little) and DCBA
(little/little) for a two-register value.
"""

WriteMode = Literal["auto", "single", "multiple"]
"""How a writable register field is written to the wire.

``"auto"`` (the default) picks the function code by payload width: FC06
(write-single-register) for a one-word value, FC16 (write-multiple-registers)
otherwise. Some devices contradict that heuristic, so override it per field:
``"single"`` always uses FC06 (for a device that rejects multi-register writes;
only valid for a one-word value), ``"multiple"`` always uses FC16 (for a device
that honours only FC16, even for a single register).
"""
