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

BitSpace = Literal["coil", "discrete"]
"""Which bit space a field is read from: coil (FC01) or discrete input (FC02)."""

SocketFraming = Literal["socket", "rtu", "ascii"]
"""Wire framing for a TCP/UDP connection: native Modbus (MBAP), RTU or ASCII."""

SerialFraming = Literal["rtu", "ascii"]
"""Wire framing for a serial connection: binary RTU or ASCII."""


def swap_bytes(word: int) -> int:
    """Exchange the two bytes of a 16-bit register word."""
    return ((word & 0xFF) << 8) | ((word >> 8) & 0xFF)
