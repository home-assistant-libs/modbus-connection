"""Read-planning internals shared by Component and ComponentGroup.

Groups field read targets into as few Modbus block reads as possible and scatters
the results back. Not part of the public API — use :class:`Component` /
:class:`ComponentGroup`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, NamedTuple

from ..decode import decode_int16
from ..exceptions import ModbusExceptionError
from .fields import CoilField, RegisterField

if TYPE_CHECKING:
    from .._protocol import ModbusUnit

_MAX_GAP = 8  # merge registers/coils less than this many addresses apart
_MAX_SPAN = 100  # but never read a block wider than this

Range = tuple[int, int]  # an inclusive (low, high) readable address range


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
