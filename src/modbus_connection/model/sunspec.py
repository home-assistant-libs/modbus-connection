"""SunSpec point types as ready-made model fields.

`SunSpec <https://sunspec.org>`_ defines a standard Modbus information model used
by most PV inverters, meters and batteries. Each point has a fixed data type and
a reserved *unimplemented* value the device sends when the point is absent. These
factories build model field descriptors with the right width, sign and sentinel,
so an unimplemented point decodes to ``None`` automatically — the same fields you
would otherwise hand-roll with the generic factories, minus the boilerplate.

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
points decode to their raw integer by default; pass an ``IntEnum`` / ``IntFlag``
to map them to members natively (a value with no member decodes to ``None``,
warned once).

A SunSpec device advertises which models it implements: :func:`scan`
walks the model chain at the device's base address and returns where each model
sits, and :class:`SunSpecComponent` is the base for a component placed at a
discovered model, verifying the model header on every update.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, IntFlag
from typing import TYPE_CHECKING, Any, Final, overload

from ..decode import decode_uint32
from .component import Component
from .fields import (
    Eui48Field,
    FloatField,
    IPv4Field,
    IPv6Field,
    NumberField,
    StringField,
    WriteValidator,
)

if TYPE_CHECKING:
    from .._protocol import ModbusUnit

__all__ = [
    "SunSpecComponent",
    "SunSpecError",
    "SunSpecModel",
    "acc16",
    "acc32",
    "acc64",
    "bitfield16",
    "bitfield32",
    "bitfield64",
    "scan",
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
    writable: bool | WriteValidator,
    unit: str | None,
) -> NumberField[float]:
    return NumberField(
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
    writable: bool | WriteValidator = False,
    unit: str | None = None,
) -> NumberField[float]:
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
    writable: bool | WriteValidator = False,
    unit: str | None = None,
) -> NumberField[float]:
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
    writable: bool | WriteValidator = False,
    unit: str | None = None,
) -> NumberField[float]:
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
    writable: bool | WriteValidator = False,
    unit: str | None = None,
) -> NumberField[float]:
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
    writable: bool | WriteValidator = False,
    unit: str | None = None,
) -> NumberField[float]:
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
    writable: bool | WriteValidator = False,
    unit: str | None = None,
) -> NumberField[float]:
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
) -> NumberField[int]:
    """A 16-bit accumulator — a monotonic counter (0 means not accumulated)."""
    return NumberField(
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
) -> NumberField[int]:
    """A 32-bit accumulator over two registers (0 means not accumulated)."""
    return NumberField(
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
) -> NumberField[int]:
    """A 64-bit accumulator over four registers (0 means not accumulated)."""
    return NumberField(
        address,
        count=4,
        signed=False,
        nan=_ACC_NAN,
        scale=scale,
        stride=stride,
        unit=unit,
    )


def sunssf(address: int, *, stride: int = 0) -> NumberField[int]:
    """A scale-factor point: a signed int16 power-of-ten exponent."""
    return NumberField(address, count=1, signed=True, nan=_INT16_NAN, stride=stride)


@overload
def enum16(
    address: int, *, stride: int = 0, writable: bool | WriteValidator = False
) -> NumberField[int]: ...
@overload
def enum16[E: IntEnum](
    address: int,
    enum: type[E],
    *,
    stride: int = 0,
    writable: bool | WriteValidator = False,
) -> NumberField[E]: ...
def enum16(
    address: int,
    enum: type[IntEnum] | None = None,
    *,
    stride: int = 0,
    writable: bool | WriteValidator = False,
) -> NumberField[Any]:
    """A 16-bit enumeration (unimplemented 0xFFFF).

    Pass an ``IntEnum`` to decode to its member; omit it for the raw code.
    """
    return NumberField(
        address,
        count=1,
        signed=False,
        nan=_UINT16_NAN,
        convert=enum,
        stride=stride,
        writable=writable,
    )


@overload
def enum32(
    address: int, *, stride: int = 0, writable: bool | WriteValidator = False
) -> NumberField[int]: ...
@overload
def enum32[E: IntEnum](
    address: int,
    enum: type[E],
    *,
    stride: int = 0,
    writable: bool | WriteValidator = False,
) -> NumberField[E]: ...
def enum32(
    address: int,
    enum: type[IntEnum] | None = None,
    *,
    stride: int = 0,
    writable: bool | WriteValidator = False,
) -> NumberField[Any]:
    """A 32-bit enumeration over two registers (unimplemented 0xFFFFFFFF).

    Pass an ``IntEnum`` to decode to its member; omit it for the raw code.
    """
    return NumberField(
        address,
        count=2,
        signed=False,
        nan=_UINT32_NAN,
        convert=enum,
        stride=stride,
        writable=writable,
    )


@overload
def bitfield16(
    address: int, *, stride: int = 0, writable: bool | WriteValidator = False
) -> NumberField[int]: ...
@overload
def bitfield16[F: IntFlag](
    address: int,
    flags: type[F],
    *,
    stride: int = 0,
    writable: bool | WriteValidator = False,
) -> NumberField[F]: ...
def bitfield16(
    address: int,
    flags: type[IntFlag] | None = None,
    *,
    stride: int = 0,
    writable: bool | WriteValidator = False,
) -> NumberField[Any]:
    """A 16-bit bitfield (unimplemented 0xFFFF).

    Pass an ``IntFlag`` to decode to its flags; omit it for the raw word.
    """
    return NumberField(
        address,
        count=1,
        signed=False,
        nan=_UINT16_NAN,
        convert=flags,
        stride=stride,
        writable=writable,
    )


@overload
def bitfield32(
    address: int, *, stride: int = 0, writable: bool | WriteValidator = False
) -> NumberField[int]: ...
@overload
def bitfield32[F: IntFlag](
    address: int,
    flags: type[F],
    *,
    stride: int = 0,
    writable: bool | WriteValidator = False,
) -> NumberField[F]: ...
def bitfield32(
    address: int,
    flags: type[IntFlag] | None = None,
    *,
    stride: int = 0,
    writable: bool | WriteValidator = False,
) -> NumberField[Any]:
    """A 32-bit bitfield over two registers (unimplemented 0xFFFFFFFF).

    Pass an ``IntFlag`` to decode to its flags; omit it for the raw word.
    """
    return NumberField(
        address,
        count=2,
        signed=False,
        nan=_UINT32_NAN,
        convert=flags,
        stride=stride,
        writable=writable,
    )


@overload
def bitfield64(
    address: int, *, stride: int = 0, writable: bool | WriteValidator = False
) -> NumberField[int]: ...
@overload
def bitfield64[F: IntFlag](
    address: int,
    flags: type[F],
    *,
    stride: int = 0,
    writable: bool | WriteValidator = False,
) -> NumberField[F]: ...
def bitfield64(
    address: int,
    flags: type[IntFlag] | None = None,
    *,
    stride: int = 0,
    writable: bool | WriteValidator = False,
) -> NumberField[Any]:
    """A 64-bit bitfield over four registers (unimplemented 0xFFFFFFFFFFFFFFFF).

    Pass an ``IntFlag`` to decode to its flags; omit it for the raw word.
    """
    return NumberField(
        address,
        count=4,
        signed=False,
        nan=_UINT64_NAN,
        convert=flags,
        stride=stride,
        writable=writable,
    )


def float32(
    address: int,
    *,
    stride: int = 0,
    writable: bool | WriteValidator = False,
    unit: str | None = None,
) -> FloatField:
    """An IEEE-754 single-precision point (unimplemented NaN)."""
    return FloatField(
        address,
        count=2,
        nan=_FLOAT_NAN,
        stride=stride,
        writable=writable,
        unit=unit,
    )


def float64(
    address: int,
    *,
    stride: int = 0,
    writable: bool | WriteValidator = False,
    unit: str | None = None,
) -> FloatField:
    """An IEEE-754 double-precision point over four registers (unimplemented NaN)."""
    return FloatField(
        address,
        count=4,
        nan=_FLOAT_NAN,
        stride=stride,
        writable=writable,
        unit=unit,
    )


def string(
    address: int,
    length: int,
    *,
    stride: int = 0,
    writable: bool | WriteValidator = False,
) -> StringField:
    """A fixed-length null-padded ASCII string over ``length`` registers."""
    return StringField(address, count=length, stride=stride, writable=writable)


def ipaddr(address: int, *, stride: int = 0) -> IPv4Field:
    """An IPv4 address over two registers."""
    return IPv4Field(address, count=2, stride=stride)


def ipv6addr(address: int, *, stride: int = 0) -> IPv6Field:
    """An IPv6 address over eight registers."""
    return IPv6Field(address, count=8, stride=stride)


def eui48(address: int, *, stride: int = 0) -> Eui48Field:
    """An EUI-48 / MAC address over three registers."""
    return Eui48Field(address, count=3, stride=stride)


# -- model discovery -----------------------------------------------------------

_SUNSPEC_MARKER: Final = 0x53756E53  # "SunS"
_END_MODEL_ID: Final = 0xFFFF
# Sanity limit against malformed maps sending the chain walk astray.
_MAX_MODELS: Final = 100


class SunSpecError(Exception):
    """Raised when a device does not behave like a SunSpec device."""


@dataclass(frozen=True)
class SunSpecModel:
    """Location of a SunSpec model in the register map.

    ``address`` points at the 2-register model header (model ID, length);
    ``length`` is the number of data registers following the header.
    """

    model_id: int
    address: int
    length: int


class SunSpecComponent(Component):
    """A discovered SunSpec model, placed at its address and header-checked.

    Subclasses declare their fields relative to the model start: the
    2-register header sits at 0/1, the data block starts at 2. The header is
    verified against the discovered model on every update, own or pooled
    through a ``ComponentGroup`` - devices shift the register map when a
    configuration change resizes a model, and a mismatch raises
    :class:`SunSpecError` so the owner can re-discover.
    """

    model_id = uint16(0)
    model_length = uint16(1)

    def __init__(self, unit: ModbusUnit, model: SunSpecModel) -> None:
        """Initialize the component at the discovered model's address."""
        super().__init__(unit, base_offset=model.address)
        self._model = model

    def notify(self) -> None:
        """Verify the read-back model header, then fire the update listeners."""
        if (
            self.model_id != self._model.model_id
            or self.model_length != self._model.length
        ):
            raise SunSpecError(
                f"{type(self).__name__} header mismatch:"
                f" expected {self._model.model_id}/{self._model.length},"
                f" read {self.model_id}/{self.model_length}"
                " - the register map has changed"
            )
        super().notify()

    def __repr__(self) -> str:
        """Return the component's field values."""
        values = ", ".join(
            f"{name}={getattr(self, name)!r}"
            for name in self._register_fields
            if name not in ("model_id", "model_length")
        )
        return f"{type(self).__name__}({values})"


async def scan(
    unit: ModbusUnit, base_address: int
) -> dict[int, list[SunSpecModel]]:
    """Walk the SunSpec model chain and return the discovered models by ID.

    ``base_address`` is the 0-based register address of the map's ``"SunS"``
    marker - the SunSpec spec sanctions 0, 40000 and 50000, and an
    integration knows which one its manufacturer uses. Raises
    :class:`SunSpecError` when the marker is missing or the chain doesn't
    terminate.

    The same model ID can occur more than once in a chain (e.g. several
    meters), so each ID maps to its occurrences in chain order.
    """
    marker = await unit.read_holding_registers(base_address, 2)
    if decode_uint32(marker) != _SUNSPEC_MARKER:
        raise SunSpecError(f"No SunSpec marker found at register {base_address}")

    models: dict[int, list[SunSpecModel]] = {}
    address = base_address + 2
    for _ in range(_MAX_MODELS):
        model_id, length = await unit.read_holding_registers(address, 2)
        if model_id == _END_MODEL_ID:
            return models
        models.setdefault(model_id, []).append(
            SunSpecModel(model_id=model_id, address=address, length=length)
        )
        address += 2 + length
    raise SunSpecError(f"Model chain not terminated after {_MAX_MODELS} models")
