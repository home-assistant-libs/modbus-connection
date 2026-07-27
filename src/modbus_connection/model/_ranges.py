"""Operations on readable address-range maps."""

from __future__ import annotations

from collections.abc import Iterable

from ._const import _RANGE_ATTR, Range, Space


def _range_of(address: int, ranges: tuple[Range, ...] | None) -> Range | None:
    """The readable range containing ``address``, or ``None``."""
    if ranges is None:
        return None
    for low, high in ranges:
        if low <= address <= high:
            return (low, high)
    return None


def _shift_ranges(
    ranges: tuple[Range, ...] | None, offset: int
) -> tuple[Range, ...] | None:
    """Move readable ranges by ``offset``, like the addresses they constrain."""
    if ranges is None or offset == 0:
        return ranges
    return tuple((low + offset, high + offset) for low, high in ranges)


def _validate_ranges(ranges: tuple[Range, ...]) -> None:
    """Validate readable ranges.

    Raises ``ValueError`` for reversed or overlapping ranges.
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


def _ranges_excluding(
    intervals: Iterable[Range], excluded: set[int]
) -> tuple[Range, ...]:
    """Split ``intervals`` around every excluded address, dropping empty runs.

    Each ``(low, high)`` interval is cut at every excluded address it covers,
    so the result only ever *splits* the input — it never merges across an
    interval boundary. Used to narrow a component's readable ranges to the
    addresses a device actually serves.
    """
    result: list[Range] = []
    for low, high in intervals:
        start = low
        for cut in sorted(address for address in excluded if low <= address <= high):
            if cut > start:
                result.append((start, cut - 1))
            start = cut + 1
        if start <= high:
            result.append((start, high))
    return tuple(result)


def _merge_range_maps(
    space: Space, declared: Iterable[tuple[Range, ...] | None], *, whose: str
) -> tuple[Range, ...] | None:
    """Merge one space's readable maps into the map they jointly describe.

    The maps come from parts of one device — components at different offsets each
    describe their own part — so they are merged, and an unset one adds no
    constraint. Two that cover the same addresses differently describe the device
    two ways, which is a conflict.

    Raises ``ValueError`` if the maps conflict.
    """
    constrained = {ranges for ranges in declared if ranges is not None}
    if not constrained:
        return None
    if len(constrained) == 1:
        return next(iter(constrained))
    merged = tuple(sorted({r for ranges in constrained for r in ranges}))
    try:
        _validate_ranges(merged)
    except ValueError as err:
        raise ValueError(
            f"{whose} must agree on {_RANGE_ATTR[space]} where their maps overlap, "
            f"but got conflicting values: {sorted(constrained)}"
        ) from err
    return merged
