"""SunSpec point types as ready-made model fields.

`SunSpec <https://sunspec.org>`_ defines a standard Modbus information model used
by most PV inverters, meters and batteries. Each point has a fixed data type and
a reserved *unimplemented* value the device sends when the point is absent. These
factories build :class:`modbus_connection.model.RegisterField` descriptors with
the right width, sign and sentinel, so an unimplemented point decodes to
``None`` automatically — the same fields you would otherwise hand-roll with the
generic factories, minus the boilerplate.

Scaled points reference a scale-factor (``sunssf``) register: pass
``scale_register=`` its address and the value is returned as ``raw * 10**sf``,
with ``sf`` read alongside on each update::

    from modbus_connection.model import Component
    from modbus_connection.model.sunspec import acc32, int16, sunssf, uint16

    class Inverter(Component):
        a = uint16(2, scale_register=5)   # AC current, scaled by A_SF
        a_sf = sunssf(5)
        wh = acc32(8)                     # lifetime energy, Wh

Word order is big-endian throughout, per the SunSpec spec. Enum and bitfield
points decode to their raw integer — wrap them in a ``@property`` in your
component to map to an ``enum.Enum`` or ``enum.IntFlag``.
"""

from __future__ import annotations

from enum import IntEnum, IntFlag
from ipaddress import IPv4Address, IPv6Address
from typing import Any, overload

from . import RegisterField

__all__ = [
    "acc16",
    "acc32",
    "acc64",
    "bitfield16",
    "bitfield32",
    "bitfield64",
    "enum16",
    "enum32",
    "eui48",
    "float32",
    "float64",
    "int16",
    "int32",
    "int64",
    "ipaddr",
    "ipv6addr",
    "string",
    "sunssf",
    "uint16",
    "uint32",
    "uint64",
]

# Per-type "unimplemented" / "not accumulated" sentinels (SunSpec spec).
_INT16_NAN = 0x8000
_UINT16_NAN = 0xFFFF
_INT32_NAN = 0x8000_0000
_UINT32_NAN = 0xFFFF_FFFF
_INT64_NAN = 0x8000_0000_0000_0000
_UINT64_NAN = 0xFFFF_FFFF_FFFF_FFFF
_ACC_NAN = 0x0  # acc16/32/64: 0 means "not accumulated"
_FLOAT_NAN = 0x7FC0_0000  # any NaN; used as a flag so float fields decode NaN to None


def _scaled(
    address: int,
    *,
    count: int,
    signed: bool,
    nan: int,
    scale: float,
    scale_register: int | None,
    scale_register_stride: int,
    stride: int,
    writable: bool,
    unit: str | None,
) -> RegisterField[float]:
    return RegisterField(
        address,
        count=count,
        signed=signed,
        nan=nan,
        scale=scale,
        scale_register=scale_register,
        scale_register_stride=scale_register_stride,
        stride=stride,
        writable=writable,
        unit=unit,
    )


def int16(
    address: int,
    *,
    scale: float = 1.0,
    scale_register: int | None = None,
    scale_register_stride: int = 0,
    stride: int = 0,
    writable: bool = False,
    unit: str | None = None,
) -> RegisterField[float]:
    """A signed 16-bit point (unimplemented 0x8000)."""
    return _scaled(
        address,
        count=1,
        signed=True,
        nan=_INT16_NAN,
        scale=scale,
        scale_register=scale_register,
        scale_register_stride=scale_register_stride,
        stride=stride,
        writable=writable,
        unit=unit,
    )


def uint16(
    address: int,
    *,
    scale: float = 1.0,
    scale_register: int | None = None,
    scale_register_stride: int = 0,
    stride: int = 0,
    writable: bool = False,
    unit: str | None = None,
) -> RegisterField[float]:
    """An unsigned 16-bit point (unimplemented 0xFFFF)."""
    return _scaled(
        address,
        count=1,
        signed=False,
        nan=_UINT16_NAN,
        scale=scale,
        scale_register=scale_register,
        scale_register_stride=scale_register_stride,
        stride=stride,
        writable=writable,
        unit=unit,
    )


def int32(
    address: int,
    *,
    scale: float = 1.0,
    scale_register: int | None = None,
    scale_register_stride: int = 0,
    stride: int = 0,
    writable: bool = False,
    unit: str | None = None,
) -> RegisterField[float]:
    """A signed 32-bit point over two registers (unimplemented 0x80000000)."""
    return _scaled(
        address,
        count=2,
        signed=True,
        nan=_INT32_NAN,
        scale=scale,
        scale_register=scale_register,
        scale_register_stride=scale_register_stride,
        stride=stride,
        writable=writable,
        unit=unit,
    )


def uint32(
    address: int,
    *,
    scale: float = 1.0,
    scale_register: int | None = None,
    scale_register_stride: int = 0,
    stride: int = 0,
    writable: bool = False,
    unit: str | None = None,
) -> RegisterField[float]:
    """An unsigned 32-bit point over two registers (unimplemented 0xFFFFFFFF)."""
    return _scaled(
        address,
        count=2,
        signed=False,
        nan=_UINT32_NAN,
        scale=scale,
        scale_register=scale_register,
        scale_register_stride=scale_register_stride,
        stride=stride,
        writable=writable,
        unit=unit,
    )


def int64(
    address: int,
    *,
    scale: float = 1.0,
    scale_register: int | None = None,
    scale_register_stride: int = 0,
    stride: int = 0,
    writable: bool = False,
    unit: str | None = None,
) -> RegisterField[float]:
    """A signed 64-bit point over four registers (unimplemented 0x8000…)."""
    return _scaled(
        address,
        count=4,
        signed=True,
        nan=_INT64_NAN,
        scale=scale,
        scale_register=scale_register,
        scale_register_stride=scale_register_stride,
        stride=stride,
        writable=writable,
        unit=unit,
    )


def uint64(
    address: int,
    *,
    scale: float = 1.0,
    scale_register: int | None = None,
    scale_register_stride: int = 0,
    stride: int = 0,
    writable: bool = False,
    unit: str | None = None,
) -> RegisterField[float]:
    """An unsigned 64-bit point over four registers (unimplemented 0xFFFF…)."""
    return _scaled(
        address,
        count=4,
        signed=False,
        nan=_UINT64_NAN,
        scale=scale,
        scale_register=scale_register,
        scale_register_stride=scale_register_stride,
        stride=stride,
        writable=writable,
        unit=unit,
    )


def acc16(
    address: int, *, scale: float = 1.0, stride: int = 0, unit: str | None = None
) -> RegisterField[int]:
    """A 16-bit accumulator — a monotonic counter (0 means not accumulated)."""
    return RegisterField(
        address,
        count=1,
        signed=False,
        nan=_ACC_NAN,
        scale=scale,
        stride=stride,
        unit=unit,
    )


def acc32(
    address: int, *, scale: float = 1.0, stride: int = 0, unit: str | None = None
) -> RegisterField[int]:
    """A 32-bit accumulator over two registers (0 means not accumulated)."""
    return RegisterField(
        address,
        count=2,
        signed=False,
        nan=_ACC_NAN,
        scale=scale,
        stride=stride,
        unit=unit,
    )


def acc64(
    address: int, *, scale: float = 1.0, stride: int = 0, unit: str | None = None
) -> RegisterField[int]:
    """A 64-bit accumulator over four registers (0 means not accumulated)."""
    return RegisterField(
        address,
        count=4,
        signed=False,
        nan=_ACC_NAN,
        scale=scale,
        stride=stride,
        unit=unit,
    )


def sunssf(address: int, *, stride: int = 0) -> RegisterField[int]:
    """A scale-factor point: a signed int16 power-of-ten exponent."""
    return RegisterField(address, count=1, signed=True, nan=_INT16_NAN, stride=stride)


@overload
def enum16(
    address: int, *, stride: int = 0, writable: bool = False
) -> RegisterField[int]: ...
@overload
def enum16[E: IntEnum](
    address: int, enum: type[E], *, stride: int = 0, writable: bool = False
) -> RegisterField[E]: ...
def enum16(
    address: int,
    enum: type[IntEnum] | None = None,
    *,
    stride: int = 0,
    writable: bool = False,
) -> RegisterField[Any]:
    """A 16-bit enumeration (unimplemented 0xFFFF).

    Pass an ``IntEnum`` to decode to its member; omit it for the raw code.
    """
    return RegisterField(
        address,
        count=1,
        signed=False,
        nan=_UINT16_NAN,
        enum_type=enum,
        stride=stride,
        writable=writable,
    )


@overload
def enum32(
    address: int, *, stride: int = 0, writable: bool = False
) -> RegisterField[int]: ...
@overload
def enum32[E: IntEnum](
    address: int, enum: type[E], *, stride: int = 0, writable: bool = False
) -> RegisterField[E]: ...
def enum32(
    address: int,
    enum: type[IntEnum] | None = None,
    *,
    stride: int = 0,
    writable: bool = False,
) -> RegisterField[Any]:
    """A 32-bit enumeration over two registers (unimplemented 0xFFFFFFFF).

    Pass an ``IntEnum`` to decode to its member; omit it for the raw code.
    """
    return RegisterField(
        address,
        count=2,
        signed=False,
        nan=_UINT32_NAN,
        enum_type=enum,
        stride=stride,
        writable=writable,
    )


@overload
def bitfield16(
    address: int, *, stride: int = 0, writable: bool = False
) -> RegisterField[int]: ...
@overload
def bitfield16[F: IntFlag](
    address: int, flags: type[F], *, stride: int = 0, writable: bool = False
) -> RegisterField[F]: ...
def bitfield16(
    address: int,
    flags: type[IntFlag] | None = None,
    *,
    stride: int = 0,
    writable: bool = False,
) -> RegisterField[Any]:
    """A 16-bit bitfield (unimplemented 0xFFFF).

    Pass an ``IntFlag`` to decode to its flags; omit it for the raw word.
    """
    return RegisterField(
        address,
        count=1,
        signed=False,
        nan=_UINT16_NAN,
        enum_type=flags,
        stride=stride,
        writable=writable,
    )


@overload
def bitfield32(
    address: int, *, stride: int = 0, writable: bool = False
) -> RegisterField[int]: ...
@overload
def bitfield32[F: IntFlag](
    address: int, flags: type[F], *, stride: int = 0, writable: bool = False
) -> RegisterField[F]: ...
def bitfield32(
    address: int,
    flags: type[IntFlag] | None = None,
    *,
    stride: int = 0,
    writable: bool = False,
) -> RegisterField[Any]:
    """A 32-bit bitfield over two registers (unimplemented 0xFFFFFFFF).

    Pass an ``IntFlag`` to decode to its flags; omit it for the raw word.
    """
    return RegisterField(
        address,
        count=2,
        signed=False,
        nan=_UINT32_NAN,
        enum_type=flags,
        stride=stride,
        writable=writable,
    )


@overload
def bitfield64(
    address: int, *, stride: int = 0, writable: bool = False
) -> RegisterField[int]: ...
@overload
def bitfield64[F: IntFlag](
    address: int, flags: type[F], *, stride: int = 0, writable: bool = False
) -> RegisterField[F]: ...
def bitfield64(
    address: int,
    flags: type[IntFlag] | None = None,
    *,
    stride: int = 0,
    writable: bool = False,
) -> RegisterField[Any]:
    """A 64-bit bitfield over four registers (unimplemented 0xFFFFFFFFFFFFFFFF).

    Pass an ``IntFlag`` to decode to its flags; omit it for the raw word.
    """
    return RegisterField(
        address,
        count=4,
        signed=False,
        nan=_UINT64_NAN,
        enum_type=flags,
        stride=stride,
        writable=writable,
    )


def float32(
    address: int, *, stride: int = 0, writable: bool = False, unit: str | None = None
) -> RegisterField[float]:
    """An IEEE-754 single-precision point (unimplemented NaN)."""
    return RegisterField(
        address,
        kind="float",
        count=2,
        nan=_FLOAT_NAN,
        stride=stride,
        writable=writable,
        unit=unit,
    )


def float64(
    address: int, *, stride: int = 0, writable: bool = False, unit: str | None = None
) -> RegisterField[float]:
    """An IEEE-754 double-precision point over four registers (unimplemented NaN)."""
    return RegisterField(
        address,
        kind="float",
        count=4,
        nan=_FLOAT_NAN,
        stride=stride,
        writable=writable,
        unit=unit,
    )


def string(
    address: int, length: int, *, stride: int = 0, writable: bool = False
) -> RegisterField[str]:
    """A fixed-length null-padded ASCII string over ``length`` registers."""
    return RegisterField(
        address, kind="string", count=length, stride=stride, writable=writable
    )


def ipaddr(address: int, *, stride: int = 0) -> RegisterField[IPv4Address]:
    """An IPv4 address over two registers."""
    return RegisterField(address, kind="ipaddr", count=2, stride=stride)


def ipv6addr(address: int, *, stride: int = 0) -> RegisterField[IPv6Address]:
    """An IPv6 address over eight registers."""
    return RegisterField(address, kind="ipv6addr", count=8, stride=stride)


def eui48(address: int, *, stride: int = 0) -> RegisterField[str]:
    """An EUI-48 / MAC address over three registers."""
    return RegisterField(address, kind="eui48", count=3, stride=stride)
