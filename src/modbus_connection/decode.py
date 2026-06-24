"""Decode Modbus register words into Python values.

Pure and backend-neutral: feed these the ``list[int]`` a raw ``ModbusUnit`` read
returns and get a typed Python value back. They cover the SunSpec point types
(see :mod:`modbus_connection.model.sunspec`). The model layer builds on them;
callers who don't model a whole device can use them directly::

    words = await unit.read_holding_registers(0x10, 2)
    power = decode_uint32(words)

Byte order within each register is always big-endian (the Modbus convention);
``word_order`` selects the order of the registers themselves for multi-register
values.
"""

from __future__ import annotations

import ipaddress
import struct

from ._types import WordOrder

__all__ = [
    "combine_words",
    "decode_eui48",
    "decode_float32",
    "decode_float64",
    "decode_int",
    "decode_int16",
    "decode_int32",
    "decode_int64",
    "decode_ipaddr",
    "decode_ipv6addr",
    "decode_scaled_sum",
    "decode_string",
    "decode_uint16",
    "decode_uint32",
    "decode_uint64",
]


def combine_words(words: list[int], *, word_order: WordOrder = "big") -> int:
    """Pack register words into one unsigned integer per ``word_order``."""
    ordered = words if word_order == "big" else list(reversed(words))
    raw = 0
    for word in ordered:
        raw = (raw << 16) | (word & 0xFFFF)
    return raw


def _signed(raw: int, bits: int) -> int:
    return raw - (1 << bits) if raw >= 1 << (bits - 1) else raw


def decode_int(words: list[int], *, signed: bool, word_order: WordOrder = "big") -> int:
    """Decode register words into a (signed or unsigned) integer of any width."""
    raw = combine_words(words, word_order=word_order)
    return _signed(raw, 16 * len(words)) if signed else raw


def decode_uint16(words: list[int]) -> int:
    """Decode one register as an unsigned 16-bit integer."""
    return combine_words(words)


def decode_int16(words: list[int]) -> int:
    """Decode one register as a signed 16-bit integer."""
    return _signed(combine_words(words), 16)


def decode_uint32(words: list[int], *, word_order: WordOrder = "big") -> int:
    """Decode two registers as an unsigned 32-bit integer."""
    return combine_words(words, word_order=word_order)


def decode_int32(words: list[int], *, word_order: WordOrder = "big") -> int:
    """Decode two registers as a signed 32-bit integer."""
    return _signed(combine_words(words, word_order=word_order), 32)


def decode_uint64(words: list[int], *, word_order: WordOrder = "big") -> int:
    """Decode four registers as an unsigned 64-bit integer."""
    return combine_words(words, word_order=word_order)


def decode_int64(words: list[int], *, word_order: WordOrder = "big") -> int:
    """Decode four registers as a signed 64-bit integer."""
    return _signed(combine_words(words, word_order=word_order), 64)


def decode_float32(words: list[int], *, word_order: WordOrder = "big") -> float:
    """Decode two registers as an IEEE-754 single-precision float."""
    ordered = words if word_order == "big" else list(reversed(words))
    raw = b"".join((w & 0xFFFF).to_bytes(2, "big") for w in ordered)
    return struct.unpack(">f", raw)[0]


def decode_float64(words: list[int], *, word_order: WordOrder = "big") -> float:
    """Decode four registers as an IEEE-754 double-precision float."""
    ordered = words if word_order == "big" else list(reversed(words))
    raw = b"".join((w & 0xFFFF).to_bytes(2, "big") for w in ordered)
    return struct.unpack(">d", raw)[0]


def decode_string(words: list[int]) -> str:
    """Decode registers as a null-padded ASCII string (two characters per word)."""
    raw = b"".join((w & 0xFFFF).to_bytes(2, "big") for w in words)
    return raw.decode("ascii", errors="ignore").rstrip("\x00")


def decode_scaled_sum(words: list[int], magnitudes: tuple[int, ...]) -> int:
    """Sum consecutive registers, each weighted by a magnitude.

    For counters a device spreads across registers of rising magnitude — e.g. a
    meter exposing Wh, kWh and MWh in three consecutive registers you add up:
    ``decode_scaled_sum(words, (1, 1000, 1_000_000))`` returns the total in Wh.
    """
    return sum((w & 0xFFFF) * m for w, m in zip(words, magnitudes, strict=True))


def decode_ipaddr(words: list[int]) -> ipaddress.IPv4Address:
    """Decode two registers as an IPv4 address (SunSpec ``ipaddr``)."""
    return ipaddress.IPv4Address(combine_words(words))


def decode_ipv6addr(words: list[int]) -> ipaddress.IPv6Address:
    """Decode eight registers as an IPv6 address (SunSpec ``ipv6addr``)."""
    return ipaddress.IPv6Address(combine_words(words))


def decode_eui48(words: list[int]) -> str:
    """Decode three registers as a colon-separated EUI-48 / MAC address."""
    octets = (combine_words(words) & ((1 << 48) - 1)).to_bytes(6, "big")
    return ":".join(f"{b:02x}" for b in octets)
