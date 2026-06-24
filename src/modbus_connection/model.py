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

import struct
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, overload

from ._types import WordOrder
from .exceptions import ModbusExceptionError

if TYPE_CHECKING:
    from ._protocol import ModbusUnit

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
    word as-is) or ``"float"`` (IEEE-754 over two registers). A value spanning
    several registers sets ``count`` (and ``word_order``); ``nan`` is an optional
    sentinel that decodes to ``None``. ``level_coil`` names a coil that is set to
    ``False`` before a write (for devices with a write-unlock/override coil).
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
    ) -> None:
        self.address = address
        self.scale = scale
        self.signed = signed
        self.writable = writable
        self.nan = nan
        self.kind = kind  # number | raw | float
        # Number of 16-bit registers this value spans (2 for uint32/float32/...).
        self.count = count
        self.word_order = word_order
        self.stride = stride
        self.unit = unit
        # A coil set to False before writing this field, for devices with a
        # write-unlock/override coil; None if no such coil is needed.
        self.level_coil = level_coil
        self.level_coil_stride = level_coil_stride
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

    def _combine(self, words: list[int]) -> int:
        """Pack the field's registers into one integer (per ``word_order``)."""
        ordered = words if self.word_order == "big" else list(reversed(words))
        raw = 0
        for word in ordered:
            raw = (raw << 16) | (word & 0xFFFF)
        return raw

    def decode(self, words: list[int]) -> Any:
        """Decode this field's ``count`` register words into its Python value."""
        if self.kind == "float":
            ordered = words if self.word_order == "big" else list(reversed(words))
            raw_bytes = b"".join((w & 0xFFFF).to_bytes(2, "big") for w in ordered)
            value = struct.unpack(">f", raw_bytes)[0]
            return value * self.scale if self.scale != 1.0 else value
        raw = self._combine(words)
        if self.kind == "raw":
            return raw  # the word(s) as-is: no NaN, sign, or scaling
        if self.nan is not None and raw == self.nan:
            return None
        bits = 16 * self.count
        if self.signed and raw >= 1 << (bits - 1):
            raw -= 1 << bits
        value = raw * self.scale
        return int(value) if self._decimals == 0 else round(value, self._decimals)

    def encode(self, value: Any) -> list[int]:
        """Encode a Python value into this field's ``count`` register words."""
        if self.kind == "float":
            raw_bytes = struct.pack(">f", float(value))
            words = [
                int.from_bytes(raw_bytes[i : i + 2], "big")
                for i in range(0, len(raw_bytes), 2)
            ]
            return words if self.word_order == "big" else list(reversed(words))
        raw = round(value / self.scale) if self.scale != 1.0 else int(value)
        if raw < 0:
            raw += 1 << (16 * self.count)
        words = [
            (raw >> (16 * (self.count - 1 - i))) & 0xFFFF for i in range(self.count)
        ]
        return words if self.word_order == "big" else list(reversed(words))


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
    unit: str | None = None,
) -> RegisterField[float]:
    """A scaled numeric register (e.g. a 0.1-scaled temperature or voltage)."""
    return RegisterField(
        address,
        scale=scale,
        signed=signed,
        nan=nan,
        stride=stride,
        writable=writable,
        level_coil=level_coil,
        level_coil_stride=level_coil_stride,
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
    unit: str | None = None,
) -> RegisterField[int]:
    """An unscaled integer register (counts, percentages, addresses)."""
    return RegisterField(
        address,
        scale=1.0,
        signed=signed,
        nan=nan,
        stride=stride,
        writable=writable,
        level_coil=level_coil,
        level_coil_stride=level_coil_stride,
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


# A read target: (absolute address, field, the component store to write into).
RegisterItem = tuple[int, "RegisterField[Any]", dict[str, Any]]
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


async def _bulk_read_registers(
    unit: ModbusUnit,
    items: list[RegisterItem],
    ranges: tuple[Range, ...] | None = None,
) -> None:
    """Read every ``(address, field, store)`` in as few Modbus calls as possible.

    Targets are pooled across whatever components are passed in, so adjacent
    registers — even ones belonging to different sub-systems — are fetched
    together, and a multi-register value is always kept within one block.
    ``ranges`` (the device's readable address ranges) keeps reads from crossing an
    unreadable gap. Each field's decoded value lands in its ``store`` under
    ``field.name``; a Modbus exception covering a block sets those fields to
    ``None`` (other errors propagate so the caller can mark the device down).
    """
    if not items:
        return
    by_address: dict[int, list[tuple[RegisterField[Any], dict[str, Any]]]] = {}
    spans: list[tuple[int, int]] = []
    for address, field, store in items:
        by_address.setdefault(address, []).append((field, store))
        spans.append((address, field.count))
    for start, count in _plan_blocks(spans, ranges):
        try:
            words = await unit.read_holding_registers(start, count)
        except ModbusExceptionError:
            for offset in range(count):
                for field, store in by_address.get(start + offset, ()):
                    store[field.name] = None
            continue
        for offset in range(count):
            for field, store in by_address.get(start + offset, ()):
                store[field.name] = field.decode(words[offset : offset + field.count])


async def _bulk_read_coils(
    unit: ModbusUnit,
    items: list[CoilItem],
    ranges: tuple[Range, ...] | None = None,
) -> None:
    """Read coil ``(address, field, store)`` targets in as few calls as possible."""
    if not items:
        return
    by_address: dict[int, list[tuple[CoilField, dict[str, Any]]]] = {}
    for address, field, store in items:
        by_address.setdefault(address, []).append((field, store))
    for start, count in _plan_blocks(((address, 1) for address in by_address), ranges):
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
        """This component's register read targets (absolute address, field, store)."""
        return [
            (self._address(f), f, self._values) for f in self._register_fields.values()
        ]

    def coil_items(self) -> list[CoilItem]:
        """This component's coil read targets (absolute address, field, store)."""
        return [(self._address(f), f, self._coils) for f in self._coil_fields.values()]

    def notify(self) -> None:
        """Fire every registered update listener."""
        for listener in list(self._listeners):
            listener()

    async def async_update(self) -> None:
        """Read this component's registers and coils, then notify listeners.

        Reads only this sub-system's own registers, so it can refresh on its own.
        A device that owns several components can instead pool their
        :meth:`register_items` / :meth:`coil_items` into one bulk read.
        """
        await _bulk_read_registers(
            self._unit, self.register_items(), self.register_ranges
        )
        await _bulk_read_coils(self._unit, self.coil_items(), self.coil_ranges)
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
    """
    items = list(components)
    register_items = [item for c in items for item in c.register_items()]
    coil_items = [item for c in items for item in c.coil_items()]
    await _bulk_read_registers(unit, register_items, register_ranges)
    await _bulk_read_coils(unit, coil_items, coil_ranges)
    for component in items:
        component.notify()
