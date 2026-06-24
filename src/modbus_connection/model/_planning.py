"""Read-planning internals shared by Component and ComponentGroup.

Groups field read targets into as few Modbus block reads as possible and scatters
the results back. Not part of the public API — use :class:`Component` /
:class:`ComponentGroup`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from ..decode import decode_int16
from ..exceptions import ModbusExceptionError
from .fields import CoilField, RegisterField

if TYPE_CHECKING:
    from .._protocol import ModbusUnit

_MAX_GAP = 8  # merge registers/coils less than this many addresses apart
# Never read a block wider than this. 125 is the Modbus per-request ceiling for
# read-holding (FC03) and read-input (FC04) registers (0x7D).
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


def _plan_register_blocks(
    items: list[RegisterItem],
    ranges_by_space: dict[RegisterSpace, tuple[Range, ...] | None],
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
        space: _plan_blocks(_register_spans(space_items), ranges_by_space.get(space))
        for space, space_items in by_space.items()
    }


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
    readers: dict[RegisterSpace, Callable[[int, int], Awaitable[list[int]]]] = {
        "holding": unit.read_holding_registers,
        "input": unit.read_input_registers,
    }
    # Keyed by (space, address): input and holding share the same address numbers
    # but are distinct registers, so they must not collide here.
    words: dict[tuple[RegisterSpace, int], int] = {}
    failed: set[tuple[RegisterSpace, int]] = set()
    for space, space_blocks in blocks.items():
        read = readers[space]
        for start, count in space_blocks:
            try:
                got = await read(start, count)
            except ModbusExceptionError:
                failed.update((space, start + offset) for offset in range(count))
                continue
            for offset in range(count):
                words[(space, start + offset)] = got[offset]
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
