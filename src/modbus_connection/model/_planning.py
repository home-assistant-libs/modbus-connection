"""Plan and execute pooled component reads."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from ..decode import decode_int16
from ..exceptions import BlockReadError, ModbusExceptionError
from ._const import _MAX_GAP, _MAX_SPAN, Range, Raw, Space
from ._ranges import _range_of, _validate_ranges
from .fields import RegisterField, _BitField

if TYPE_CHECKING:
    from .._protocol import ModbusUnit


class ReadItem(NamedTuple):
    """One read target: where to read, what field, and where to store the value."""

    address: int  # absolute start address of the field's own registers/bit
    field: RegisterField[Any] | _BitField
    store: dict[str, Any]  # the component store decoded values land in
    space: Space  # the address space to read this field from
    scale_address: int | None = None  # absolute address of the scale register


def _item_field(item: ReadItem) -> RegisterField[Any] | _BitField:
    """Return the field stored in a read item."""
    return cast("RegisterField[Any] | _BitField", item.field)


def _plan_blocks(
    spans: Iterable[tuple[int, int]],
    ranges: tuple[Range, ...] | None = None,
    *,
    max_gap: int = _MAX_GAP,
    max_span: int = _MAX_SPAN,
) -> list[tuple[int, int]]:
    """Group address spans into read blocks.

    Raises ``ValueError`` for invalid ranges, a span wider than ``max_span``, or
    a span crossing a readable-range boundary.
    """
    if ranges is not None:
        _validate_ranges(ranges)
    ordered = sorted(set(spans))
    if not ordered:
        return []
    for address, width in ordered:
        if width > max_span:
            raise ValueError(
                f"a field spanning {width} registers exceeds the "
                f"{max_span}-register read limit"
            )
        end = address + width - 1
        # A field that starts inside a readable range and ends outside it (or in
        # the next one) cannot be read at all: the layout says the device answers
        # up to the range's high and also puts a field past it.
        if ranges is not None and _range_of(address, ranges) != _range_of(end, ranges):
            raise ValueError(
                f"a field at address {address} spanning {width} registers "
                f"({address}-{end}) crosses a readable range boundary"
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
    """Store the targets and blocks for one component read."""

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
        """Plan block reads covering ``items``."""
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
        """Read and decode every block.

        Raises ``BlockReadError`` if the device rejects a block.
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
    """Share read-plan execution between component types."""

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
        """Read all targets and optionally return raw values."""
        if self._plan is None:
            self._plan = self._build_plan()
        raw = await self._plan.execute(self._unit, collect_raw=collect_raw)
        _merge_raw(raw, await self._refresh_repeating_groups(collect_raw=collect_raw))
        if notify:
            self.notify()
        return raw

    async def async_read_raw(self) -> dict[str, dict[int, int | bool]]:
        """Read and return every target keyed by address space and address.

        Raises ``BlockReadError`` if the device rejects a block.
        """
        raw = await self._refresh(collect_raw=True)
        return {space: dict(sorted(values.items())) for space, values in raw.items()}
