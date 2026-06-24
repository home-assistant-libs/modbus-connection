"""A small device-modelling framework over the ``ModbusUnit`` protocol.

Map a device's registers and coils to typed Python attributes, then read the
whole device (or one sub-system) in as few Modbus calls as possible. It is
backend-neutral: it talks only to a ``ModbusUnit``, so it runs over pymodbus,
tmodbus, or the in-memory mock.

A ``Component`` is a sub-system whose attributes are ``RegisterField`` /
``CoilField`` descriptors (usually built with the typed factories below)::

    from modbus_connection.model import Component, gauge, integer, coil

    class Meter(Component):
        voltage = gauge(0, 0.1, unit="V")
        current = gauge(1, 0.1, unit="A")
        energy = uint32(2, unit="Wh")
        relay = coil(0, writable=True)

    meter = Meter(unit)
    await meter.async_update()
    meter.voltage            # float | None

Only very generic field types ship here: scaled / unscaled integers, raw words,
and 32-bit / float values. Device-specific shaping (enums, packed dates/times,
sentinel handling beyond a single ``nan`` value) belongs in the consumer, done
with a private field plus a normal ``@property`` so static typing stays exact::

    _mode_raw = integer(5)

    @property
    def mode(self) -> Mode | None:
        raw = self._mode_raw
        return Mode(raw) if raw is not None else None

Reads are pooled into block reads. A device may pass its readable address
``ranges`` so the planner merges only within a range and never reads across an
unreadable gap.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterable
from enum import Enum
from typing import TYPE_CHECKING, Any, NamedTuple, overload

from .._types import WordOrder
from ..decode import (
    combine_words,
    decode_eui48,
    decode_float32,
    decode_float64,
    decode_int,
    decode_int16,
    decode_ipaddr,
    decode_ipv6addr,
    decode_scaled_sum,
    decode_string,
)
from ..encode import encode_float32, encode_float64, encode_int, encode_string
from ..exceptions import ModbusExceptionError

if TYPE_CHECKING:
    from .._protocol import ModbusUnit

_LOGGER = logging.getLogger(__name__)

# (enum class, raw value) pairs we have already warned about, so an unrecognized
# enum code is logged only once per distinct value rather than on every poll.
_warned_unknown_enum: set[tuple[type, int]] = set()

_MAX_GAP = 8  # merge registers/coils less than this many addresses apart
_MAX_SPAN = 100  # but never read a block wider than this

UpdateListener = Callable[[], None]
Range = tuple[int, int]  # an inclusive (low, high) readable address range

__all__ = [
    "CoilField",
    "Component",
    "Range",
    "RegisterField",
    "UpdateListener",
    "async_update_all",
    "coil",
    "float32",
    "gauge",
    "int32",
    "integer",
    "raw_register",
    "scaled_sum",
    "uint32",
]


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


class RegisterItem(NamedTuple):
    """A register read target: where to read, what field, and where to store it."""

    address: int  # absolute start address of the field's own registers
    field: RegisterField[Any]
    store: dict[str, Any]  # the component store decoded values land in
    scale_address: int | None  # absolute address of the field's sunssf register


CoilItem = tuple[int, "CoilField", dict[str, Any]]


def _range_of(address: int, ranges: tuple[Range, ...] | None) -> Range | None:
    """The readable range containing ``address``, or ``None``."""
    if ranges is None:
        return None
    for low, high in ranges:
        if low <= address <= high:
            return (low, high)
    return None


def _plan_blocks(
    spans: Iterable[tuple[int, int]],
    ranges: tuple[Range, ...] | None = None,
) -> list[tuple[int, int]]:
    """Group ``(start_address, width)`` spans into ``(start, count)`` read blocks.

    A multi-register value is never split across blocks (each span is placed
    whole) and a block never grows past ``_MAX_SPAN`` registers.

    Without ``ranges`` (the generic default), spans no more than ``_MAX_GAP``
    apart share a block. With ``ranges`` — the device's readable address ranges —
    spans merge only when they sit in the *same* range (the gap between them is
    then readable too), and never across a range boundary; reads are still clipped
    to the addresses actually used.
    """
    ordered = sorted(set(spans))
    if not ordered:
        return []
    blocks: list[tuple[int, int]] = []
    block_start, width = ordered[0]
    block_end = block_start + width - 1  # last (inclusive) address covered so far
    block_range = _range_of(block_start, ranges)
    for address, width in ordered[1:]:
        end = address + width - 1
        if ranges is None:
            mergeable = address - block_end <= _MAX_GAP
        else:
            address_range = _range_of(address, ranges)
            mergeable = address_range is not None and address_range == block_range
        if mergeable and end - block_start + 1 <= _MAX_SPAN:
            block_end = max(block_end, end)
        else:
            blocks.append((block_start, block_end - block_start + 1))
            block_start, block_end = address, end
            block_range = _range_of(address, ranges)
    blocks.append((block_start, block_end - block_start + 1))
    return blocks


def _register_spans(items: list[RegisterItem]) -> list[tuple[int, int]]:
    """The ``(address, width)`` spans a register read must cover (values + sunssf)."""
    spans: list[tuple[int, int]] = []
    for item in items:
        spans.append((item.address, item.field.count))
        if item.scale_address is not None:
            spans.append((item.scale_address, 1))
    return spans


async def _bulk_read_registers(
    unit: ModbusUnit,
    items: list[RegisterItem],
    blocks: list[tuple[int, int]],
) -> None:
    """Read every register target over the precomputed ``blocks``.

    ``blocks`` is the read plan (from :func:`_plan_blocks` over
    :func:`_register_spans`); it is passed in rather than recomputed so a polling
    component plans its static layout once. Targets are pooled, so adjacent
    registers — even ones belonging to different sub-systems — are fetched
    together, and a multi-register value is always kept within one block. A
    field's ``sunssf`` scale register (if any) is read in the same pass and
    applied at decode. Each field's decoded value lands in its ``store`` under
    ``field.name``; a Modbus exception covering a field's registers sets it to
    ``None`` (other errors propagate so the caller can mark the device down).
    """
    if not items:
        return
    words_by_address: dict[int, int] = {}
    failed: set[int] = set()
    for start, count in blocks:
        try:
            words = await unit.read_holding_registers(start, count)
        except ModbusExceptionError:
            failed.update(range(start, start + count))
            continue
        for offset in range(count):
            words_by_address[start + offset] = words[offset]
    for item in items:
        field = item.field
        addresses = range(item.address, item.address + field.count)
        if any(address in failed for address in addresses):
            item.store[field.name] = None
            continue
        scale_exponent: int | None = None
        if item.scale_address is not None:
            if item.scale_address in failed:
                item.store[field.name] = None
                continue
            scale_exponent = decode_int16([words_by_address[item.scale_address]])
        field_words = [words_by_address[address] for address in addresses]
        item.store[field.name] = field.decode(field_words, scale_exponent)


async def _bulk_read_coils(
    unit: ModbusUnit,
    items: list[CoilItem],
    blocks: list[tuple[int, int]],
) -> None:
    """Read coil targets over the precomputed ``blocks`` (plan passed in, see above)."""
    if not items:
        return
    by_address: dict[int, list[tuple[CoilField, dict[str, Any]]]] = {}
    for address, field, store in items:
        by_address.setdefault(address, []).append((field, store))
    for start, count in blocks:
        try:
            bits = await unit.read_coils(start, count)
        except ModbusExceptionError:
            for offset in range(count):
                for field, store in by_address.get(start + offset, ()):
                    store[field.name] = None
            continue
        for offset in range(count):
            bit = bool(bits[offset])
            for field, store in by_address.get(start + offset, ()):
                store[field.name] = bit


class Component:
    """A device sub-system whose attributes map to registers and coils.

    Subclasses declare ``RegisterField`` / ``CoilField`` descriptors (usually via
    the typed factories). Each component reads only its own registers, so it can
    refresh independently; listeners registered via :meth:`add_update_listener`
    fire after each update (so one entity per component can subscribe).

    A device that pools several components into one update fetches them together;
    declare :attr:`register_ranges` / :attr:`coil_ranges` (e.g. from the device's
    datasheet) — as class attributes on a subclass or per instance — so reads
    never cross an unreadable gap.

    The read plan (which blocks to fetch) is derived from the static field layout
    and cached on first :meth:`async_update`, so each subsequent poll reuses it
    rather than re-planning. Set ``register_ranges`` / ``coil_ranges`` before the
    first update.
    """

    _register_fields: dict[str, RegisterField[Any]] = {}
    _coil_fields: dict[str, CoilField] = {}

    # The device's readable address ranges; None falls back to gap-based planning.
    # Override on a subclass (or set per instance) to constrain reads to the
    # addresses the device actually answers.
    register_ranges: tuple[Range, ...] | None = None
    coil_ranges: tuple[Range, ...] | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        registers: dict[str, RegisterField[Any]] = {}
        coils: dict[str, CoilField] = {}
        for klass in reversed(cls.__mro__):
            for name, value in vars(klass).items():
                if isinstance(value, RegisterField):
                    registers[name] = value
                elif isinstance(value, CoilField):
                    coils[name] = value
        cls._register_fields = registers
        cls._coil_fields = coils

    def __init__(self, unit: ModbusUnit, index: int = 1) -> None:
        self._unit = unit
        self._index = index
        self._values: dict[str, Any] = {}
        self._coils: dict[str, bool | None] = {}
        self._listeners: list[UpdateListener] = []
        # Read targets and their block plan are static; built once, then reused.
        self._register_items: list[RegisterItem] | None = None
        self._coil_items: list[CoilItem] | None = None
        self._register_blocks: list[tuple[int, int]] | None = None
        self._coil_blocks: list[tuple[int, int]] | None = None

    def _address(self, field: RegisterField[Any] | CoilField) -> int:
        return field.address + field.stride * (self._index - 1)

    # -- listeners -----------------------------------------------------------

    def add_update_listener(self, listener: UpdateListener) -> Callable[[], None]:
        """Register a callback fired after each update; returns an unsubscribe."""
        self._listeners.append(listener)

        def remove() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return remove

    # -- update --------------------------------------------------------------

    def register_items(self) -> list[RegisterItem]:
        """This component's register read targets, scale registers resolved (cached)."""
        if self._register_items is None:
            items = []
            for field in self._register_fields.values():
                scale_address = None
                if field.scale_register is not None:
                    scale_address = (
                        field.scale_register
                        + field.scale_register_stride * (self._index - 1)
                    )
                items.append(
                    RegisterItem(
                        self._address(field), field, self._values, scale_address
                    )
                )
            self._register_items = items
        return self._register_items

    def coil_items(self) -> list[CoilItem]:
        """This component's coil read targets (absolute address, field, store)."""
        if self._coil_items is None:
            self._coil_items = [
                (self._address(f), f, self._coils) for f in self._coil_fields.values()
            ]
        return self._coil_items

    def notify(self) -> None:
        """Fire every registered update listener."""
        for listener in list(self._listeners):
            listener()

    async def async_update(self) -> None:
        """Read this component's registers and coils, then notify listeners.

        Reads only this sub-system's own registers, so it can refresh on its own.
        A device that owns several components can instead pool their
        :meth:`register_items` / :meth:`coil_items` into one bulk read. The block
        plan is built on the first call and reused on later polls.
        """
        register_items = self.register_items()
        coil_items = self.coil_items()
        if self._register_blocks is None:
            self._register_blocks = _plan_blocks(
                _register_spans(register_items), self.register_ranges
            )
        if self._coil_blocks is None:
            self._coil_blocks = _plan_blocks(
                ((address, 1) for address, _, _ in coil_items), self.coil_ranges
            )
        await _bulk_read_registers(self._unit, register_items, self._register_blocks)
        await _bulk_read_coils(self._unit, coil_items, self._coil_blocks)
        self.notify()

    # -- writes --------------------------------------------------------------

    async def write(self, field: str, value: Any) -> None:
        """Write a writable register or coil by attribute name.

        If the field has a ``level_coil`` (a write-unlock/override coil), it is
        first set to ``False`` so the device accepts the write.
        """
        if field in self._register_fields:
            register = self._register_fields[field]
            if not register.writable:
                raise AttributeError(f"{field} is read-only")
            await self._unlock(register)
            address = self._address(register)
            words = register.encode(value)
            if len(words) == 1:
                await self._unit.write_register(address, words[0])
            else:
                await self._unit.write_registers(address, words)
        elif field in self._coil_fields:
            coil_field = self._coil_fields[field]
            if not coil_field.writable:
                raise AttributeError(f"{field} is read-only")
            await self._unlock(coil_field)
            await self._unit.write_coil(self._address(coil_field), bool(value))
        else:
            raise AttributeError(f"unknown field {field!r}")

    async def _unlock(self, field: RegisterField[Any] | CoilField) -> None:
        """Release the field's write-unlock coil (set it to False)."""
        if field.level_coil is None:
            return
        address = field.level_coil + field.level_coil_stride * (self._index - 1)
        await self._unit.write_coil(address, False)


async def async_update_all(
    unit: ModbusUnit,
    components: Iterable[Component],
    register_ranges: tuple[Range, ...] | None = None,
    coil_ranges: tuple[Range, ...] | None = None,
) -> None:
    """Refresh several components that share one unit in pooled block reads.

    Their register and coil targets are merged into a single consolidated set of
    reads — adjacent registers from different components are fetched together —
    rather than each component querying on its own. Listeners fire per component
    afterwards. Pass the device's readable ``ranges`` to avoid crossing gaps.

    The plan spans whichever components are passed, so it is built per call (over
    their cached targets) rather than stored on any one component.
    """
    items = list(components)
    register_items = [item for c in items for item in c.register_items()]
    coil_items = [item for c in items for item in c.coil_items()]
    register_blocks = _plan_blocks(_register_spans(register_items), register_ranges)
    coil_blocks = _plan_blocks(
        ((address, 1) for address, _, _ in coil_items), coil_ranges
    )
    await _bulk_read_registers(unit, register_items, register_blocks)
    await _bulk_read_coils(unit, coil_items, coil_blocks)
    for component in items:
        component.notify()
