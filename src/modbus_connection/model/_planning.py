"""Read-planning internals shared by Component and ComponentGroup.

Groups field read targets into as few Modbus block reads as possible and scatters
the results back. Not part of the public API — use :class:`Component` /
:class:`ComponentGroup`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
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


class RegisterItem(NamedTuple):
    """A register read target: where to read, what field, and where to store it."""

    address: int  # absolute start address of the field's own registers
    field: RegisterField[Any]
    store: dict[str, Any]  # the component store decoded values land in
    scale_address: int | None  # absolute address of the field's scale register
    space: RegisterSpace  # the register space to read this field from


# A bit read target (the field carries its own ``space``): address, field, store.
BitItem = tuple[int, "_BitField", dict[str, Any]]


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


def _register_spans(items: list[RegisterItem]) -> list[tuple[int, int]]:
    """The ``(address, width)`` spans a register read must cover (values + scales)."""
    spans: list[tuple[int, int]] = []
    for item in items:
        # ``item.field`` reads the stored RegisterField off the NamedTuple; since
        # RegisterField is a descriptor, mypy applies its instance-access overload
        # and widens the value to ``T | None``, so pin it back to the real type.
        field = cast("RegisterField[Any]", item.field)
        spans.append((item.address, field.count))
        if item.scale_address is not None:
            spans.append((item.scale_address, 1))
    return spans


def _plan_register_blocks(
    items: list[RegisterItem],
    ranges_by_space: dict[RegisterSpace, tuple[Range, ...] | None],
    *,
    max_gap: int = _MAX_GAP,
    max_span: int = _MAX_SPAN,
) -> dict[RegisterSpace, list[tuple[int, int]]]:
    """Plan read blocks separately per register space; spaces never merge.

    Items are partitioned by :attr:`RegisterItem.space` and each partition planned
    on its own. ``ranges_by_space`` gives each space's readable address ranges.
    """
    by_space: dict[RegisterSpace, list[RegisterItem]] = {}
    for item in items:
        by_space.setdefault(item.space, []).append(item)
    return {
        space: _plan_blocks(
            _register_spans(space_items),
            ranges_by_space.get(space),
            max_gap=max_gap,
            max_span=max_span,
        )
        for space, space_items in by_space.items()
    }


def _plan_bit_blocks(
    items: list[BitItem],
    ranges_by_space: dict[BitSpace, tuple[Range, ...] | None],
    *,
    max_gap: int = _MAX_GAP,
    max_span: int = _MAX_SPAN,
) -> dict[BitSpace, list[tuple[int, int]]]:
    """Plan bit read blocks per space; coils and discrete inputs never merge."""
    by_space: dict[BitSpace, list[tuple[int, int]]] = {}
    for address, field, _store in items:
        by_space.setdefault(field.space, []).append((address, 1))
    return {
        space: _plan_blocks(
            spans, ranges_by_space.get(space), max_gap=max_gap, max_span=max_span
        )
        for space, spans in by_space.items()
    }


async def _read_blocks_by_space[S: str, E](
    readers: dict[S, Callable[[int, int], Awaitable[list[E]]]],
    blocks: dict[S, list[tuple[int, int]]],
) -> dict[tuple[S, int], E]:
    """Read every block per space, keyed by ``(space, address)``.

    The shared core of the bulk readers: each space's blocks are read with that
    space's reader. A ``ModbusExceptionError`` on a block is re-raised as a
    :class:`BlockReadError` naming the block that failed; any other error propagates
    unchanged so the caller can mark the device down.
    """
    values: dict[tuple[S, int], E] = {}
    for space, space_blocks in blocks.items():
        read = readers[space]
        for start, count in space_blocks:
            try:
                got = await read(start, count)
            except ModbusExceptionError as err:
                raise BlockReadError(space, start, count, err.exception_code) from err
            for offset in range(count):
                values[(space, start + offset)] = got[offset]
    return values


async def _bulk_read_registers(
    unit: ModbusUnit,
    items: list[RegisterItem],
    blocks: dict[RegisterSpace, list[tuple[int, int]]],
) -> None:
    """Read every register target over the precomputed per-space ``blocks``.

    ``blocks`` is the read plan (from :func:`_plan_register_blocks`); it is passed
    in rather than recomputed so a polling component plans its static layout once.
    Each space's blocks are read with the matching function — ``read_input_registers``
    (FC04) for ``"input"``, ``read_holding_registers`` (FC03) for ``"holding"`` —
    and a field's scale register (read from the same space) is fetched
    in the same pass and applied at decode. Each field's decoded value lands in
    its ``store`` under ``field.name``. A block answering with a Modbus exception
    raises :class:`BlockReadError` (other errors propagate so the caller can mark
    the device down).
    """
    if not items:
        return
    words = await _read_blocks_by_space(
        {"holding": unit.read_holding_registers, "input": unit.read_input_registers},
        blocks,
    )
    for item in items:
        field = cast("RegisterField[Any]", item.field)  # descriptor widening, see above
        keys = [(item.space, item.address + offset) for offset in range(field.count)]
        scale_exponent: int | None = None
        if item.scale_address is not None:
            scale_key = (item.space, item.scale_address)
            scale_exponent = decode_int16([words[scale_key]])
        field_words = [words[key] for key in keys]
        item.store[field.name] = field.decode(field_words, scale_exponent)


async def _bulk_read_bits(
    unit: ModbusUnit,
    items: list[BitItem],
    blocks: dict[BitSpace, list[tuple[int, int]]],
) -> None:
    """Read coil (FC01) and discrete-input (FC02) targets over the given blocks.

    The bit counterpart of :func:`_bulk_read_registers`; a block answering with a
    Modbus exception raises :class:`BlockReadError`.
    """
    if not items:
        return
    bits = await _read_blocks_by_space(
        {"coil": unit.read_coils, "discrete": unit.read_discrete_inputs},
        blocks,
    )
    for address, field, store in items:
        store[field.name] = bool(bits[(field.space, address)])
