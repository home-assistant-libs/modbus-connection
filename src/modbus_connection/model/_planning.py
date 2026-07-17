"""Read planning and execution shared by the component classes.

Groups field read targets into as few Modbus block reads as possible and
scatters the results back. Two ideas live here:

- :class:`ReadPlan` — the read targets (:class:`ReadItem`) of one poll and the
  block reads that cover them, buildable from the targets plus the device's
  readable ranges, and executable against a ``ModbusUnit``.
- :class:`_Readable` — the template shared by :class:`Component`,
  :class:`ManualComponent` and :class:`ComponentGroup`: cache a plan, execute
  it, run the ``repeating_group`` second pass, notify listeners.

Not part of the public API — use the component classes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, cast

from .._types import BitSpace
from ..decode import decode_int16
from ..exceptions import BlockReadError, ModbusExceptionError
from .fields import RegisterField, _BitField

if TYPE_CHECKING:
    from .._protocol import ModbusUnit

# Defaults for Component.max_gap / Component.max_span (overridable per device).
_MAX_GAP = 16  # gap-based planning: merge spans within this many addresses
# Default block-width cap. 125 is the Modbus per-request ceiling for read-holding
# (FC03) / read-input (FC04); a device whose gateway caps lower can override it.
_MAX_SPAN = 125

Range = tuple[int, int]  # an inclusive (low, high) readable address range

# Which register space a field is read from: input (FC04) or holding (FC03).
# They are separate address spaces — input 507 is not holding 507 — so blocks
# from different spaces are never merged into one read.
RegisterSpace = Literal["input", "holding"]

# Any of the four Modbus address spaces a read target can live in.
Space = RegisterSpace | BitSpace

# Raw read results, grouped ``{space: {address: value}}`` — words for the
# register spaces, booleans for the bit spaces.
Raw = dict[str, dict[int, int | bool]]


class ReadItem(NamedTuple):
    """One read target: where to read, what field, and where to store the value."""

    address: int  # absolute start address of the field's own registers/bit
    field: RegisterField[Any] | _BitField
    store: dict[str, Any]  # the component store decoded values land in
    space: Space  # the address space to read this field from
    scale_address: int | None = None  # absolute address of the scale register


def _item_field(item: ReadItem) -> RegisterField[Any] | _BitField:
    """The field stored in a :class:`ReadItem`, with its real type.

    ``item.field`` reads the field off the NamedTuple; since the fields are
    descriptors, mypy applies their instance-access overload and widens the
    value to the *attribute* type, so pin it back to the stored object.
    """
    return cast("RegisterField[Any] | _BitField", item.field)


def _range_of(address: int, ranges: tuple[Range, ...] | None) -> Range | None:
    """The readable range containing ``address``, or ``None``."""
    if ranges is None:
        return None
    for low, high in ranges:
        if low <= address <= high:
            return (low, high)
    return None


def _validate_ranges(ranges: tuple[Range, ...]) -> None:
    """Reject reversed or overlapping readable ranges.

    :func:`_range_of` maps an address to the *first* range that contains it, so
    overlapping ranges would make block-merge decisions depend on declaration
    order, and a reversed ``(low > high)`` range can never match. Both are config
    errors, so fail loudly rather than mis-plan silently.
    """
    for low, high in ranges:
        if low > high:
            raise ValueError(f"readable range ({low}, {high}) is reversed: low > high")
    ordered = sorted(ranges)
    for (a_low, a_high), (b_low, b_high) in zip(ordered, ordered[1:], strict=False):
        if b_low <= a_high:
            raise ValueError(
                f"readable ranges overlap: ({a_low}, {a_high}) and ({b_low}, {b_high})"
            )


def _plan_blocks(
    spans: Iterable[tuple[int, int]],
    ranges: tuple[Range, ...] | None = None,
    *,
    max_gap: int = _MAX_GAP,
    max_span: int = _MAX_SPAN,
) -> list[tuple[int, int]]:
    """Group ``(start_address, width)`` spans into ``(start, count)`` read blocks.

    A multi-register value is never split across blocks (each span is placed
    whole) and a block never grows past ``max_span`` registers.

    Without ``ranges`` (the generic default), spans no more than ``max_gap`` apart
    share a block. With ``ranges`` — the device's readable address ranges — spans
    merge only when they sit in the *same* range (the gap between them is then
    readable too), and never across a range boundary; reads are still clipped to
    the addresses actually used.
    """
    if ranges is not None:
        _validate_ranges(ranges)
    ordered = sorted(set(spans))
    if not ordered:
        return []
    for _, width in ordered:
        if width > max_span:
            raise ValueError(
                f"a field spanning {width} registers exceeds the "
                f"{max_span}-register read limit"
            )
    blocks: list[tuple[int, int]] = []
    block_start, width = ordered[0]
    block_end = block_start + width - 1  # last (inclusive) address covered so far
    block_range = _range_of(block_start, ranges)
    for address, width in ordered[1:]:
        end = address + width - 1
        if ranges is None:
            mergeable = address - block_end <= max_gap
        else:
            address_range = _range_of(address, ranges)
            mergeable = address_range is not None and address_range == block_range
        if mergeable and end - block_start + 1 <= max_span:
            block_end = max(block_end, end)
        else:
            blocks.append((block_start, block_end - block_start + 1))
            block_start, block_end = address, end
            block_range = _range_of(address, ranges)
    blocks.append((block_start, block_end - block_start + 1))
    return blocks


def _reader(
    unit: ModbusUnit, space: Space
) -> Callable[[int, int], Awaitable[Sequence[int | bool]]]:
    """The unit's read call for one address space (FC03/FC04/FC01/FC02)."""
    match space:
        case "holding":
            return unit.read_holding_registers
        case "input":
            return unit.read_input_registers
        case "coil":
            return unit.read_coils
        case "discrete":
            return unit.read_discrete_inputs


class ReadPlan(NamedTuple):
    """The read targets of one poll and the block reads that cover them.

    Built once from the targets plus the device's readable ranges
    (:meth:`build`) and executed on every poll (:meth:`execute`). Each of the
    four Modbus spaces is planned and read on its own — blocks never merge
    across spaces.
    """

    items: list[ReadItem]
    blocks: dict[Space, list[tuple[int, int]]]

    @classmethod
    def build(
        cls,
        items: Iterable[ReadItem],
        ranges: Mapping[Space, tuple[Range, ...] | None],
        *,
        max_gap: int = _MAX_GAP,
        max_span: int = _MAX_SPAN,
    ) -> ReadPlan:
        """Plan block reads covering ``items`` (values and scale registers).

        ``ranges`` gives each space's readable address ranges; a space left out
        (or ``None``) falls back to gap-based planning.
        """
        items = list(items)
        spans: dict[Space, list[tuple[int, int]]] = {}
        for item in items:
            spans.setdefault(item.space, []).append(
                (item.address, _item_field(item).count)
            )
            if item.scale_address is not None:
                spans[item.space].append((item.scale_address, 1))
        return cls(
            items,
            {
                space: _plan_blocks(
                    space_spans, ranges.get(space), max_gap=max_gap, max_span=max_span
                )
                for space, space_spans in spans.items()
            },
        )

    async def execute(self, unit: ModbusUnit, *, collect_raw: bool = False) -> Raw:
        """Read every block, decode each item into its store, return the raw values.

        Each space's blocks are read with the matching function call — FC03/FC04
        for holding/input registers, FC01/FC02 for coils/discrete inputs — and a
        field's scale register (read from the same space) is applied at decode.
        Each field's decoded value lands in its ``store`` under ``field.name``.
        A block answering with a Modbus exception raises :class:`BlockReadError`
        naming the block that failed; any other error propagates unchanged so
        the caller can mark the device down.

        With ``collect_raw`` the raw words and bits read are also returned,
        grouped ``{space: {address: value}}``, for ``async_read_raw`` to expose;
        the normal decode-only poll leaves it off and gets an empty dict.
        """
        if not self.items:
            return {}
        values: dict[tuple[Space, int], int | bool] = {}
        for space, space_blocks in self.blocks.items():
            read = _reader(unit, space)
            for start, count in space_blocks:
                try:
                    got = await read(start, count)
                except ModbusExceptionError as err:
                    raise BlockReadError(
                        space, start, count, err.exception_code
                    ) from err
                for offset in range(count):
                    values[(space, start + offset)] = got[offset]
        for item in self.items:
            field = _item_field(item)
            words = [
                values[(item.space, item.address + offset)]
                for offset in range(field.count)
            ]
            scale_exponent: int | None = None
            if item.scale_address is not None:
                scale_exponent = decode_int16(
                    [values[(item.space, item.scale_address)]]
                )
            item.store[field.name] = field.decode(words, scale_exponent)
        if not collect_raw:
            return {}
        raw: Raw = {}
        for (space, address), value in values.items():
            raw.setdefault(space, {})[address] = value
        return raw


def _merge_raw(
    into: dict[str, dict[int, int | bool]],
    more: Mapping[str, Mapping[int, int | bool]],
) -> None:
    """Merge a raw ``{space: {address: value}}`` map into an accumulator in place."""
    for space, values in more.items():
        into.setdefault(space, {}).update(values)


class _Readable:
    """The refresh template shared by the three component classes.

    A subclass supplies ``_unit`` and three hooks — :meth:`_build_plan` (its
    read targets and ranges as a :class:`ReadPlan`), :meth:`notify` (fire its
    listeners) and :meth:`_refresh_repeating_groups` (the ``repeating_group``
    second pass) — and inherits the whole update cycle: the plan is built on the
    first refresh and cached until :meth:`_invalidate_plan`.
    """

    _unit: ModbusUnit
    _plan: ReadPlan | None = None

    def _build_plan(self) -> ReadPlan:
        """This object's read plan; built lazily and cached by :meth:`_refresh`."""
        raise NotImplementedError

    def notify(self) -> None:
        """Fire update listeners; provided by the subclass."""
        raise NotImplementedError

    async def _refresh_repeating_groups(self, *, collect_raw: bool) -> Raw:
        """The register-count ``repeating_group`` second pass; subclass-provided."""
        raise NotImplementedError

    def _invalidate_plan(self) -> None:
        """Drop the cached plan so the next refresh rebuilds it."""
        self._plan = None

    async def _refresh(self, *, collect_raw: bool, notify: bool = True) -> Raw:
        """Read registers, bits and repeating groups once, then notify — the core
        shared by ``async_update`` and ``async_read_raw``.

        With ``collect_raw`` the raw words and bits are merged and returned;
        without it the reads collect nothing and the returned dict is empty.
        ``notify=False`` skips the listeners, for a caller that notifies itself
        (an inner repeating pass, whose top-level read notifies once).
        """
        if self._plan is None:
            self._plan = self._build_plan()
        raw = await self._plan.execute(self._unit, collect_raw=collect_raw)
        _merge_raw(raw, await self._refresh_repeating_groups(collect_raw=collect_raw))
        if notify:
            self.notify()
        return raw

    async def async_read_raw(self) -> dict[str, dict[int, int | bool]]:
        """Read every target raw, keyed by absolute address, for diagnostics.

        Runs the same reads as ``async_update`` (pooled blocks plus the
        :func:`repeating_group` second pass) and, like an update, refreshes the
        decoded values and notifies listeners — it additionally returns the raw
        values, keyed by the four Modbus spaces: ``{space: {address: value}}``,
        words for ``"holding"`` / ``"input"`` and booleans for ``"coil"`` /
        ``"discrete"``. Raises
        :class:`~modbus_connection.exceptions.BlockReadError` like an update.
        """
        raw = await self._refresh(collect_raw=True)
        return {space: dict(sorted(values.items())) for space, values in raw.items()}
