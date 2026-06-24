"""Register / coil field descriptors and the typed factories that build them.

A field is a descriptor placed on a :class:`~modbus_connection.model.Component`
subclass; it owns the codec (how raw register words become a Python value) but
holds no per-read state. ``RegisterField`` is the abstract base — one concrete
subclass per codec (``NumberField``, ``FloatField``, ``StringField``,
``MagnitudeField``, ``RawField``, the address types). Prefer the factories
(``gauge``, ``integer``, ...) over constructing a subclass directly; they are the
named presets (width, sign, sentinel, scale) over that small set of codecs.
"""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from enum import Enum, IntEnum, IntFlag
from ipaddress import IPv4Address, IPv6Address
from typing import TYPE_CHECKING, Any, overload

from .._types import WordOrder
from ..decode import (
    combine_words,
    decode_eui48,
    decode_float32,
    decode_float64,
    decode_int,
    decode_ipaddr,
    decode_ipv6addr,
    decode_scaled_sum,
    decode_string,
)
from ..encode import encode_float32, encode_float64, encode_int, encode_string

__all__ = [
    "CoilField",
    "Eui48Field",
    "FloatField",
    "IPv4Field",
    "IPv6Field",
    "MagnitudeField",
    "NumberField",
    "RawField",
    "RegisterField",
    "StringField",
    "coil",
    "enum",
    "flags",
    "float32",
    "float64",
    "gauge",
    "int32",
    "int64",
    "integer",
    "raw_register",
    "scaled_sum",
    "string",
    "uint32",
    "uint64",
]

_LOGGER = logging.getLogger(__name__)

# (enum class, raw value) pairs we have already warned about, so an unrecognized
# enum code is logged only once per distinct value rather than on every poll.
_warned_unknown_enum: set[tuple[type, int]] = set()


def _decimals(scale: float) -> int:
    """Number of decimals implied by a scale factor (0.1 -> 1, 2.5 -> 1, 10 -> 0).

    A whole-number scale formats to no fractional digits and yields 0, so the
    result stays an ``int``; a fractional scale (whether below or above 1) keeps
    its decimals, so e.g. a 2.5 scale is rounded rather than truncated.
    """
    return max(0, len(f"{scale:.10f}".rstrip("0").split(".")[1]))


class RegisterField[T](ABC):
    """A holding register exposed as a typed attribute (returns ``T | None``).

    Abstract base: it owns the descriptor protocol and the addressing every field
    shares, and declares the codec contract (:meth:`decode` / :meth:`encode`).
    The concrete subclasses below implement one codec each.
    """

    # Set to the attribute name by __set_name__ when used as a class descriptor;
    # the default keeps decode()/logging working on an unbound field.
    name: str = ""

    def __init__(
        self,
        address: int,
        *,
        count: int = 1,
        writable: bool = False,
        stride: int = 0,
        unit: str | None = None,
        level_coil: int | None = None,
        level_coil_stride: int = 0,
        scale_register: int | None = None,
        scale_register_stride: int = 0,
    ) -> None:
        """Initialise the shared part of a field.

        Args:
            address: Holding-register address of the value's first word, before
                ``stride`` is applied. The absolute address read is
                ``address + stride * (index - 1)``.
            count: Number of 16-bit registers the value spans.
            writable: Whether :meth:`Component.write` may write this field.
            stride: Per-index address increment for a repeated block of identical
                sub-units; ``0`` means the field is at a fixed address.
            unit: Unit-of-measure label carried as metadata; not used in decoding.
            level_coil: Address of a write-unlock/override coil set to ``False``
                immediately before a write to this field; ``None`` if not needed.
            level_coil_stride: Per-index increment for ``level_coil``.
            scale_register: Address of a SunSpec ``sunssf`` register (a signed
                int16 exponent) read alongside this field and applied as
                ``value * 10**sf``; ``None`` for a static scale only.
            scale_register_stride: Per-index increment for ``scale_register``.
        """
        self.address = address
        self.count = count
        self.writable = writable
        self.stride = stride
        self.unit = unit
        # A coil set to False before writing this field, for devices with a
        # write-unlock/override coil; None if no such coil is needed.
        self.level_coil = level_coil
        self.level_coil_stride = level_coil_stride
        # Address of a sunssf register whose 10**sf scales this field; None for a
        # static scale. Read by the planner for every field.
        self.scale_register = scale_register
        self.scale_register_stride = scale_register_stride

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    if TYPE_CHECKING:

        @overload
        def __get__(self, obj: None, objtype: Any = ...) -> RegisterField[T]: ...

        @overload
        def __get__(self, obj: object, objtype: Any = ...) -> T | None: ...

    def __get__(self, obj: Any, objtype: Any = None) -> Any:
        if obj is None:
            return self
        return obj._values.get(self.name)

    @abstractmethod
    def decode(self, words: list[int], scale_exponent: int | None = None) -> Any:
        """Decode this field's ``count`` register words into its Python value.

        ``scale_exponent`` is the value of the field's ``sunssf`` register, if it
        has one; scaled fields then multiply the result by ``10**scale_exponent``.
        """

    def encode(self, value: Any) -> list[int]:
        """Encode a Python value into register words. Read-only fields raise."""
        raise NotImplementedError(f"{type(self).__name__} is read-only")


class _ScaledField[T](RegisterField[T]):
    """A field with a static ``scale``, optional ``nan`` sentinel and rounding."""

    def __init__(
        self, address: int, *, scale: float = 1.0, nan: int | None = None, **kwargs: Any
    ) -> None:
        """``scale`` multiplies the decoded number; ``nan`` decodes to ``None``."""
        super().__init__(address, **kwargs)
        self.scale = scale
        self.nan = nan
        self._decimals = _decimals(scale)

    def _scale(self, value: float, scale_exponent: int | None) -> Any:
        """Apply this field's static scale and optional 10**sf, then round."""
        factor = self.scale
        if scale_exponent is not None:
            factor *= 10.0**scale_exponent
        if factor == 1.0:
            return value  # keep ints integral when there is nothing to scale
        scaled = value * factor
        decimals = self._decimals if scale_exponent is None else _decimals(factor)
        return int(scaled) if decimals == 0 else round(scaled, decimals)


class NumberField[T](_ScaledField[T]):
    """A scaled integer, optionally signed, sentinel-checked or enum-mapped.

    ``enum_type`` maps the raw value through an ``IntEnum`` / ``IntFlag``: an
    ``IntEnum`` code with no member decodes to ``None`` (warned once per value),
    while ``IntFlag`` keeps any unknown bits.
    """

    def __init__(
        self,
        address: int,
        *,
        signed: bool = True,
        enum_type: type[Enum] | None = None,
        word_order: WordOrder = "big",
        **kwargs: Any,
    ) -> None:
        super().__init__(address, **kwargs)
        self.signed = signed
        # IntEnum / IntFlag to map the raw value through; None returns the raw int.
        self.enum_type = enum_type
        self.word_order = word_order

    def decode(self, words: list[int], scale_exponent: int | None = None) -> Any:
        raw = combine_words(words, word_order=self.word_order)
        if self.nan is not None and raw == self.nan:
            return None
        if self.enum_type is not None:
            return self._to_enum(raw)
        value = decode_int(words, signed=self.signed, word_order=self.word_order)
        return self._scale(value, scale_exponent)

    def _to_enum(self, raw: int) -> Any:
        """Map a raw value through ``enum_type``; unknown IntEnum codes warn once."""
        assert self.enum_type is not None
        try:
            return self.enum_type(raw)  # IntFlag keeps unknown bits; IntEnum may raise
        except ValueError:
            key = (self.enum_type, raw)
            if key not in _warned_unknown_enum:
                _warned_unknown_enum.add(key)
                _LOGGER.warning(
                    "Field %r: %s has no member for value %d; decoding as None",
                    self.name,
                    self.enum_type.__name__,
                    raw,
                )
            return None

    def encode(self, value: Any) -> list[int]:
        if self.scale_register is not None:
            raise NotImplementedError(
                "writing a dynamically-scaled field is unsupported"
            )
        raw = round(value / self.scale) if self.scale != 1.0 else int(value)
        return encode_int(raw, count=self.count, word_order=self.word_order)


class RawField(RegisterField[int]):
    """A raw register word (no scaling, sign handling or sentinel)."""

    def __init__(
        self, address: int, *, word_order: WordOrder = "big", **kwargs: Any
    ) -> None:
        super().__init__(address, **kwargs)
        self.word_order = word_order

    def decode(self, words: list[int], scale_exponent: int | None = None) -> int:
        return combine_words(words, word_order=self.word_order)

    def encode(self, value: Any) -> list[int]:
        return encode_int(int(value), count=self.count, word_order=self.word_order)


class FloatField(_ScaledField[float]):
    """An IEEE-754 float over two (``float32``) or four (``float64``) registers."""

    def __init__(
        self, address: int, *, word_order: WordOrder = "big", **kwargs: Any
    ) -> None:
        super().__init__(address, **kwargs)
        self.word_order = word_order

    def decode(self, words: list[int], scale_exponent: int | None = None) -> Any:
        decoder = decode_float64 if self.count == 4 else decode_float32
        value = decoder(words, word_order=self.word_order)
        if self.nan is not None and math.isnan(value):
            return None
        return self._scale(value, scale_exponent)

    def encode(self, value: Any) -> list[int]:
        if self.scale_register is not None:
            raise NotImplementedError(
                "writing a dynamically-scaled field is unsupported"
            )
        encoder = encode_float64 if self.count == 4 else encode_float32
        return encoder(value, word_order=self.word_order)


class StringField(RegisterField[str]):
    """A fixed-length null-padded ASCII string over ``count`` registers."""

    def decode(self, words: list[int], scale_exponent: int | None = None) -> str:
        return decode_string(words)

    def encode(self, value: Any) -> list[int]:
        return encode_string(value, length=self.count)


class MagnitudeField(_ScaledField[int]):
    """Consecutive registers summed by per-register weight (read-only)."""

    def __init__(
        self, address: int, magnitudes: tuple[int, ...], **kwargs: Any
    ) -> None:
        super().__init__(address, count=len(magnitudes), **kwargs)
        self.magnitudes = magnitudes

    def decode(self, words: list[int], scale_exponent: int | None = None) -> Any:
        return self._scale(decode_scaled_sum(words, self.magnitudes), scale_exponent)


class IPv4Field(RegisterField[IPv4Address]):
    """An IPv4 address over two registers (read-only)."""

    def decode(
        self, words: list[int], scale_exponent: int | None = None
    ) -> IPv4Address:
        return decode_ipaddr(words)


class IPv6Field(RegisterField[IPv6Address]):
    """An IPv6 address over eight registers (read-only)."""

    def decode(
        self, words: list[int], scale_exponent: int | None = None
    ) -> IPv6Address:
        return decode_ipv6addr(words)


class Eui48Field(RegisterField[str]):
    """An EUI-48 / MAC address over three registers (read-only)."""

    def decode(self, words: list[int], scale_exponent: int | None = None) -> str:
        return decode_eui48(words)


class CoilField:
    """A coil exposed as a ``bool | None`` attribute."""

    name: str = ""  # set by __set_name__ when used as a class descriptor

    def __init__(
        self,
        address: int,
        *,
        writable: bool = False,
        stride: int = 0,
        level_coil: int | None = None,
        level_coil_stride: int = 0,
    ) -> None:
        self.address = address
        self.writable = writable
        self.stride = stride
        self.level_coil = level_coil
        self.level_coil_stride = level_coil_stride

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    if TYPE_CHECKING:

        @overload
        def __get__(self, obj: None, objtype: Any = ...) -> CoilField: ...

        @overload
        def __get__(self, obj: object, objtype: Any = ...) -> bool | None: ...

    def __get__(self, obj: Any, objtype: Any = None) -> Any:
        if obj is None:
            return self
        return obj._coils.get(self.name)


# -- typed field factories ----------------------------------------------------


def gauge(
    address: int,
    scale: float,
    *,
    signed: bool = True,
    nan: int | None = None,
    stride: int = 0,
    writable: bool = False,
    level_coil: int | None = None,
    level_coil_stride: int = 0,
    scale_register: int | None = None,
    scale_register_stride: int = 0,
    unit: str | None = None,
) -> NumberField[float]:
    """A scaled numeric register (e.g. a 0.1-scaled temperature or voltage).

    Pass ``scale_register`` for a device whose scale factor lives in another
    register (read as a signed int16 and applied as ``10**sf``).
    """
    return NumberField(
        address,
        scale=scale,
        signed=signed,
        nan=nan,
        stride=stride,
        writable=writable,
        level_coil=level_coil,
        level_coil_stride=level_coil_stride,
        scale_register=scale_register,
        scale_register_stride=scale_register_stride,
        unit=unit,
    )


def integer(
    address: int,
    *,
    signed: bool = True,
    nan: int | None = None,
    stride: int = 0,
    writable: bool = False,
    level_coil: int | None = None,
    level_coil_stride: int = 0,
    scale_register: int | None = None,
    scale_register_stride: int = 0,
    unit: str | None = None,
) -> NumberField[int]:
    """An unscaled integer register (counts, percentages, addresses).

    Pass ``scale_register`` for a device whose scale factor lives in another
    register (read as a signed int16 and applied as ``10**sf``).
    """
    return NumberField(
        address,
        signed=signed,
        nan=nan,
        stride=stride,
        writable=writable,
        level_coil=level_coil,
        level_coil_stride=level_coil_stride,
        scale_register=scale_register,
        scale_register_stride=scale_register_stride,
        unit=unit,
    )


def raw_register(address: int, *, stride: int = 0, writable: bool = False) -> RawField:
    """A raw register word (no scaling or sign handling)."""
    return RawField(address, stride=stride, writable=writable)


def uint32(
    address: int,
    *,
    scale: float = 1.0,
    word_order: WordOrder = "big",
    stride: int = 0,
    writable: bool = False,
    unit: str | None = None,
) -> NumberField[int]:
    """An unsigned 32-bit value over two consecutive registers."""
    return NumberField(
        address,
        count=2,
        word_order=word_order,
        scale=scale,
        signed=False,
        stride=stride,
        writable=writable,
        unit=unit,
    )


def int32(
    address: int,
    *,
    scale: float = 1.0,
    word_order: WordOrder = "big",
    stride: int = 0,
    writable: bool = False,
    unit: str | None = None,
) -> NumberField[int]:
    """A signed 32-bit value over two consecutive registers."""
    return NumberField(
        address,
        count=2,
        word_order=word_order,
        scale=scale,
        signed=True,
        stride=stride,
        writable=writable,
        unit=unit,
    )


def float32(
    address: int,
    *,
    scale: float = 1.0,
    word_order: WordOrder = "big",
    stride: int = 0,
    writable: bool = False,
    unit: str | None = None,
) -> FloatField:
    """An IEEE-754 single-precision float over two consecutive registers."""
    return FloatField(
        address,
        count=2,
        word_order=word_order,
        scale=scale,
        stride=stride,
        writable=writable,
        unit=unit,
    )


def scaled_sum(
    address: int,
    magnitudes: tuple[int, ...] = (1, 1000, 1_000_000),
    *,
    scale: float = 1.0,
    stride: int = 0,
    unit: str | None = None,
) -> MagnitudeField:
    """Consecutive registers summed by weight (read-only).

    For devices that spread a counter across registers of rising magnitude — e.g.
    Stiebel Eltron energy meters expose Wh, kWh and MWh in three consecutive
    registers that you add up: ``scaled_sum(addr, (1, 1000, 1_000_000))`` reads
    all three and returns the total in Wh.
    """
    return MagnitudeField(address, magnitudes, scale=scale, stride=stride, unit=unit)


def uint64(
    address: int,
    *,
    scale: float = 1.0,
    word_order: WordOrder = "big",
    stride: int = 0,
    writable: bool = False,
    unit: str | None = None,
) -> NumberField[int]:
    """An unsigned 64-bit value over four consecutive registers."""
    return NumberField(
        address,
        count=4,
        word_order=word_order,
        scale=scale,
        signed=False,
        stride=stride,
        writable=writable,
        unit=unit,
    )


def int64(
    address: int,
    *,
    scale: float = 1.0,
    word_order: WordOrder = "big",
    stride: int = 0,
    writable: bool = False,
    unit: str | None = None,
) -> NumberField[int]:
    """A signed 64-bit value over four consecutive registers."""
    return NumberField(
        address,
        count=4,
        word_order=word_order,
        scale=scale,
        signed=True,
        stride=stride,
        writable=writable,
        unit=unit,
    )


def float64(
    address: int,
    *,
    scale: float = 1.0,
    word_order: WordOrder = "big",
    stride: int = 0,
    writable: bool = False,
    unit: str | None = None,
) -> FloatField:
    """An IEEE-754 double-precision float over four consecutive registers."""
    return FloatField(
        address,
        count=4,
        word_order=word_order,
        scale=scale,
        stride=stride,
        writable=writable,
        unit=unit,
    )


def string(
    address: int, length: int, *, stride: int = 0, writable: bool = False
) -> StringField:
    """A fixed-length null-padded ASCII string over ``length`` registers."""
    return StringField(address, count=length, stride=stride, writable=writable)


def enum[E: IntEnum](
    address: int,
    enum_type: type[E],
    *,
    count: int = 1,
    word_order: WordOrder = "big",
    nan: int | None = None,
    stride: int = 0,
    writable: bool = False,
    level_coil: int | None = None,
    level_coil_stride: int = 0,
) -> NumberField[E]:
    """An integer register mapped to an ``IntEnum`` member.

    A code with no member decodes to ``None`` (warned once per value). ``nan`` is
    an optional raw sentinel that also decodes to ``None``. ``level_coil`` names a
    write-unlock coil released before a write (for writable mode registers).
    """
    return NumberField(
        address,
        count=count,
        signed=False,
        enum_type=enum_type,
        word_order=word_order,
        nan=nan,
        stride=stride,
        writable=writable,
        level_coil=level_coil,
        level_coil_stride=level_coil_stride,
    )


def flags[F: IntFlag](
    address: int,
    flag_type: type[F],
    *,
    count: int = 1,
    word_order: WordOrder = "big",
    nan: int | None = None,
    stride: int = 0,
    writable: bool = False,
    level_coil: int | None = None,
    level_coil_stride: int = 0,
) -> NumberField[F]:
    """A bitfield register mapped to an ``IntFlag`` (unknown bits are kept).

    ``level_coil`` names a write-unlock coil released before a write.
    """
    return NumberField(
        address,
        count=count,
        signed=False,
        enum_type=flag_type,
        word_order=word_order,
        nan=nan,
        stride=stride,
        writable=writable,
        level_coil=level_coil,
        level_coil_stride=level_coil_stride,
    )


def coil(
    address: int,
    *,
    writable: bool = False,
    stride: int = 0,
    level_coil: int | None = None,
    level_coil_stride: int = 0,
) -> CoilField:
    """A coil."""
    return CoilField(
        address,
        writable=writable,
        stride=stride,
        level_coil=level_coil,
        level_coil_stride=level_coil_stride,
    )
