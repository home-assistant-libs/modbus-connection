"""Register / coil field descriptors and the typed factories that build them.

A field is a descriptor placed on a :class:`~modbus_connection.model.Component`
subclass; it owns the codec (how raw register words become a Python value) but
holds no per-read state. Prefer the factories (``gauge``, ``integer``, ...) over
constructing :class:`RegisterField` directly.
"""

from __future__ import annotations

import logging
import math
from enum import Enum
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
    "RegisterField",
    "coil",
    "float32",
    "gauge",
    "int32",
    "integer",
    "raw_register",
    "scaled_sum",
    "uint32",
]

_LOGGER = logging.getLogger(__name__)

# (enum class, raw value) pairs we have already warned about, so an unrecognized
# enum code is logged only once per distinct value rather than on every poll.
_warned_unknown_enum: set[tuple[type, int]] = set()


def _decimals(scale: float) -> int:
    """Number of decimals implied by a scale factor (0.1 -> 1, 0.01 -> 2)."""
    if scale >= 1:
        return 0
    return max(0, len(f"{scale:.10f}".rstrip("0").split(".")[1]))


class RegisterField[T]:
    """A holding register exposed as a typed attribute (returns ``T | None``).

    ``kind`` is one of ``"number"`` (scaled, optionally signed), ``"raw"`` (the
    word as-is), ``"float"`` (IEEE-754 over two or four registers), ``"string"``,
    ``"magnitudes"`` (consecutive registers summed by weight) or one of the
    address formats ``"ipaddr"`` / ``"ipv6addr"`` / ``"eui48"``. A value spanning
    several registers sets ``count`` (and ``word_order``); ``nan`` is an optional
    sentinel that decodes to ``None``.

    A SunSpec-style dynamic scale factor is given via ``scale_register``: the
    address of a ``sunssf`` (signed int16) register read alongside this field on
    each update, the value then returned as ``raw * 10**sf``. ``level_coil`` names
    a coil set to ``False`` before a write (for devices with a write-unlock coil).

    ``enum_type`` maps the raw value through an ``IntEnum`` / ``IntFlag``: an
    ``IntEnum`` code with no member decodes to ``None`` (warned once per value),
    while ``IntFlag`` keeps any unknown bits.
    """

    def __init__(
        self,
        address: int,
        *,
        scale: float = 1.0,
        signed: bool = True,
        writable: bool = False,
        nan: int | None = None,
        kind: str = "number",
        count: int = 1,
        word_order: WordOrder = "big",
        stride: int = 0,
        unit: str | None = None,
        level_coil: int | None = None,
        level_coil_stride: int = 0,
        magnitudes: tuple[int, ...] | None = None,
        scale_register: int | None = None,
        scale_register_stride: int = 0,
        enum_type: type[Enum] | None = None,
    ) -> None:
        """Build a register field. Prefer the typed factories below over this.

        Args:
            address: Holding-register address of the value's first word, before
                ``stride`` is applied. The absolute address read is
                ``address + stride * (index - 1)``.
            scale: Static multiplier applied to the decoded number (e.g. ``0.1``
                for a tenths value). Combines with ``scale_register`` if both are
                set. Applies to ``number`` / ``float`` / ``magnitudes`` kinds.
            signed: Whether the integer is two's-complement signed. Ignored by
                non-integer kinds.
            writable: Whether :meth:`Component.write` may write this field.
            nan: Raw value that decodes to ``None`` (an "unimplemented" sentinel,
                e.g. ``0x8000``). For ``float``, any non-``None`` value enables
                NaN-to-``None`` decoding instead of an exact match.
            kind: Which codec to use: ``"number"`` (scaled integer), ``"raw"``
                (the word(s) as-is, no sign/scale/nan), ``"float"`` (IEEE-754 over
                two or four registers), ``"string"``, ``"magnitudes"`` (registers
                summed by weight), or an address format ``"ipaddr"`` /
                ``"ipv6addr"`` / ``"eui48"``.
            count: Number of 16-bit registers the value spans (2 for
                uint32/float32, 4 for uint64/float64, the string/address length).
            word_order: Order of registers within a multi-register value;
                ``"big"`` is most-significant word first. Byte order within each
                register is always big-endian.
            stride: Per-index address increment for a repeated block of identical
                sub-units; see ``address``. ``0`` means the field is at a fixed
                address.
            unit: Unit-of-measure label carried as metadata; not used in decoding.
            level_coil: Address of a write-unlock/override coil set to ``False``
                immediately before a write to this field; ``None`` if not needed.
            level_coil_stride: Per-index increment for ``level_coil``.
            magnitudes: For ``kind="magnitudes"``, the per-register weights that
                are multiplied and summed (e.g. ``(1, 1000, 1_000_000)`` for
                Wh/kWh/MWh); ``count`` must equal ``len(magnitudes)``.
            scale_register: Address of a SunSpec ``sunssf`` register (a signed
                int16 exponent) read alongside this field and applied as
                ``value * 10**sf``; ``None`` for a static scale only.
            scale_register_stride: Per-index increment for ``scale_register``.
            enum_type: An ``IntEnum`` / ``IntFlag`` to map the raw value through.
                An ``IntFlag`` keeps unknown bits; an ``IntEnum`` with no member
                for the value decodes to ``None`` (warned once per value).
                ``None`` returns the raw int.
        """
        self.address = address
        self.scale = scale
        self.signed = signed
        self.writable = writable
        self.nan = nan
        self.kind = kind
        # Number of 16-bit registers this value spans (2 for uint32/float32/...).
        self.count = count
        self.word_order = word_order
        self.stride = stride
        self.unit = unit
        # A coil set to False before writing this field, for devices with a
        # write-unlock/override coil; None if no such coil is needed.
        self.level_coil = level_coil
        self.level_coil_stride = level_coil_stride
        # Per-register weights for a "magnitudes" field (e.g. Wh/kWh/MWh).
        self.magnitudes = magnitudes
        # Address of a sunssf register whose 10**sf scales this field; None for a
        # static scale.
        self.scale_register = scale_register
        self.scale_register_stride = scale_register_stride
        # IntEnum / IntFlag to map the raw value through; None returns the raw int.
        self.enum_type = enum_type
        self._decimals = _decimals(scale)

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

    # -- codec ---------------------------------------------------------------

    def decode(self, words: list[int], scale_exponent: int | None = None) -> Any:
        """Decode this field's ``count`` register words into its Python value.

        ``scale_exponent`` is the value of the field's ``sunssf`` register, if it
        has one; the result is then multiplied by ``10**scale_exponent``.
        """
        if self.kind == "string":
            return decode_string(words)
        if self.kind == "ipaddr":
            return decode_ipaddr(words)
        if self.kind == "ipv6addr":
            return decode_ipv6addr(words)
        if self.kind == "eui48":
            return decode_eui48(words)
        if self.kind == "float":
            decoder = decode_float64 if self.count == 4 else decode_float32
            value = decoder(words, word_order=self.word_order)
            if self.nan is not None and math.isnan(value):
                return None
            return self._scale(value, scale_exponent)
        raw = combine_words(words, word_order=self.word_order)
        if self.kind == "raw":
            return raw  # the word(s) as-is: no NaN, sign, or scaling
        if self.nan is not None and raw == self.nan:
            return None
        if self.enum_type is not None:
            return self._to_enum(raw)
        if self.kind == "magnitudes":
            assert self.magnitudes is not None
            return self._scale(
                decode_scaled_sum(words, self.magnitudes), scale_exponent
            )
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

    def encode(self, value: Any) -> list[int]:
        """Encode a Python value into this field's ``count`` register words."""
        if self.scale_register is not None:
            raise NotImplementedError(
                "writing a dynamically-scaled field is unsupported"
            )
        if self.kind == "float":
            encoder = encode_float64 if self.count == 4 else encode_float32
            return encoder(value, word_order=self.word_order)
        if self.kind == "string":
            return encode_string(value, length=self.count)
        if self.kind in ("magnitudes", "ipaddr", "ipv6addr", "eui48"):
            raise NotImplementedError(f"{self.kind} fields are read-only")
        raw = round(value / self.scale) if self.scale != 1.0 else int(value)
        return encode_int(raw, count=self.count, word_order=self.word_order)


class CoilField:
    """A coil exposed as a ``bool | None`` attribute."""

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
) -> RegisterField[float]:
    """A scaled numeric register (e.g. a 0.1-scaled temperature or voltage).

    Pass ``scale_register`` for a device whose scale factor lives in another
    register (read as a signed int16 and applied as ``10**sf``).
    """
    return RegisterField(
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
) -> RegisterField[int]:
    """An unscaled integer register (counts, percentages, addresses).

    Pass ``scale_register`` for a device whose scale factor lives in another
    register (read as a signed int16 and applied as ``10**sf``).
    """
    return RegisterField(
        address,
        scale=1.0,
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


def raw_register(
    address: int, *, stride: int = 0, writable: bool = False
) -> RegisterField[int]:
    """A raw register word (no scaling or sign handling)."""
    return RegisterField(address, kind="raw", stride=stride, writable=writable)


def uint32(
    address: int,
    *,
    scale: float = 1.0,
    word_order: WordOrder = "big",
    stride: int = 0,
    writable: bool = False,
    unit: str | None = None,
) -> RegisterField[int]:
    """An unsigned 32-bit value over two consecutive registers."""
    return RegisterField(
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
) -> RegisterField[int]:
    """A signed 32-bit value over two consecutive registers."""
    return RegisterField(
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
) -> RegisterField[float]:
    """An IEEE-754 single-precision float over two consecutive registers."""
    return RegisterField(
        address,
        count=2,
        word_order=word_order,
        kind="float",
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
) -> RegisterField[int]:
    """Consecutive registers summed by weight (read-only).

    For devices that spread a counter across registers of rising magnitude — e.g.
    Stiebel Eltron energy meters expose Wh, kWh and MWh in three consecutive
    registers that you add up: ``scaled_sum(addr, (1, 1000, 1_000_000))`` reads
    all three and returns the total in Wh.
    """
    return RegisterField(
        address,
        kind="magnitudes",
        count=len(magnitudes),
        magnitudes=magnitudes,
        scale=scale,
        signed=False,
        stride=stride,
        unit=unit,
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
