"""Read-planning internals shared by Component and ComponentGroup.

Groups field read targets into as few Modbus block reads as possible and scatters
the results back. Not part of the public API — use :class:`Component` /
:class:`ComponentGroup`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Hashable, Iterable
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from .._types import BitSpace
from ..decode import decode_int16
from ..exceptions import ModbusExceptionError
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
    scale_address: int | None  # absolute address of the field's sunssf register
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
    """The ``(address, width)`` spans a register read must cover (values + sunssf)."""
    spans: list[tuple[int, int]] = []
    for item in items:
        spans.append((item.address, item.field.count))
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

    Items are partitioned by their :attr:`RegisterItem.space` and each partition
    is planned on its own — an input and a holding span at numerically adjacent
    addresses land in different reads. ``ranges_by_space`` gives the readable
    address ranges for each space (a device's input and holding ranges differ).
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


async def _read_blocks_by_space[S: Hashable, E](
    readers: dict[S, Callable[[int, int], Awaitable[list[E]]]],
    blocks: dict[S, list[tuple[int, int]]],
) -> tuple[dict[tuple[S, int], E], set[tuple[S, int]]]:
    """Read every block per space, returning values and the addresses that failed.

    The shared core of the bulk readers: each space's blocks are read with that
    space's reader and the results are keyed by ``(space, address)`` — distinct
    spaces share address numbers but are different data, so the space is part of
    the key. A ``ModbusExceptionError`` on a block marks all of its addresses
    failed (so the caller stores ``None`` for any field they cover) and reading
    continues; any other error propagates so the caller can mark the device down.
    """
    values: dict[tuple[S, int], E] = {}
    failed: set[tuple[S, int]] = set()
    for space, space_blocks in blocks.items():
        read = readers[space]
        for start, count in space_blocks:
            try:
                got = await read(start, count)
            except ModbusExceptionError:
                failed.update((space, start + offset) for offset in range(count))
                continue
            for offset in range(count):
                values[(space, start + offset)] = got[offset]
    return values, failed


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
    and a field's ``sunssf`` scale register (read from the same space) is fetched
    in the same pass and applied at decode. Each field's decoded value lands in
    its ``store`` under ``field.name``; a Modbus exception covering a field's
    registers sets it to ``None`` (other errors propagate so the caller can mark
    the device down).
    """
    if not items:
        return
    words, failed = await _read_blocks_by_space(
        {"holding": unit.read_holding_registers, "input": unit.read_input_registers},
        blocks,
    )
    for item in items:
        field = item.field
        keys = [(item.space, item.address + offset) for offset in range(field.count)]
        if any(key in failed for key in keys):
            item.store[field.name] = None
            continue
        scale_exponent: int | None = None
        if item.scale_address is not None:
            scale_key = (item.space, item.scale_address)
            if scale_key in failed:
                item.store[field.name] = None
                continue
            scale_exponent = decode_int16([words[scale_key]])
        field_words = [words[key] for key in keys]
        item.store[field.name] = field.decode(field_words, scale_exponent)


async def _bulk_read_bits(
    unit: ModbusUnit,
    items: list[BitItem],
    blocks: dict[BitSpace, list[tuple[int, int]]],
) -> None:
    """Read coil (FC01) and discrete-input (FC02) targets over the given blocks.

    The bit counterpart of :func:`_bulk_read_registers`; a Modbus exception
    covering a bit sets its field to ``None``.
    """
    if not items:
        return
    bits, failed = await _read_blocks_by_space(
        {"coil": unit.read_coils, "discrete": unit.read_discrete_inputs},
        blocks,
    )
    for address, field, store in items:
        key = (field.space, address)
        store[field.name] = None if key in failed else bool(bits[key])
