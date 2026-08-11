"""Readable address-range maps and the operations on them."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from ._const import _RANGE_ATTR, Range, Space


def _range_of(address: int, ranges: tuple[Range, ...] | None) -> Range | None:
    """The readable range containing ``address``, or ``None``."""
    if ranges is None:
        return None
    for low, high in ranges:
        if low <= address <= high:
            return (low, high)
    return None


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


def _coalesce(ranges: tuple[Range, ...]) -> tuple[Range, ...]:
    """Join ranges that touch or overlap.

    Two ranges with nothing between them describe one readable run, so a read
    may span both. Only called on maps whose parts have already been checked
    for conflicts.
    """
    joined: list[Range] = []
    for low, high in sorted(ranges):
        if joined and low <= joined[-1][1] + 1:
            joined[-1] = (joined[-1][0], max(joined[-1][1], high))
        else:
            joined.append((low, high))
    return tuple(joined)


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


@dataclass(frozen=True)
class DeviceRanges:
    """A device's readable ranges per address space.

    A space mapped to ``None`` — or absent entirely — is unconstrained (planned
    gap-based). The maps live in whatever coordinate system their owner resolves
    them in; ``shift`` moves the whole device's map between systems.
    """

    maps: Mapping[Space, tuple[Range, ...] | None]

    def for_space(self, space: Space) -> tuple[Range, ...] | None:
        """The readable ranges of one space, or ``None`` if unconstrained."""
        return self.maps.get(space)

    def shift(self, offset: int) -> DeviceRanges:
        """Move every space's ranges, like the addresses they constrain."""
        if offset == 0:
            return self
        shifted: dict[Space, tuple[Range, ...] | None] = {}
        for space, ranges in self.maps.items():
            shifted[space] = (
                None
                if ranges is None
                else tuple((low + offset, high + offset) for low, high in ranges)
            )
        return DeviceRanges(shifted)

    def widened(self, claims: Mapping[Space, tuple[Range, ...]]) -> DeviceRanges:
        """Return these maps with ``claims`` folded into the spaces they cover.

        A claim says only that something reads those addresses, not that the
        device serves the span they sit in, so it is added to a map rather
        than checked against it.
        """
        if not claims:
            return self
        widened: dict[Space, tuple[Range, ...] | None] = dict(self.maps)
        for space, ranges in claims.items():
            widened[space] = _coalesce(tuple(widened.get(space) or ()) + ranges)
        return DeviceRanges(widened)

    @classmethod
    def merged(
        cls,
        maps: Iterable[DeviceRanges],
        *,
        whose: str | Callable[[Space], str],
    ) -> DeviceRanges:
        """Merge several devices' maps into the map they jointly describe.

        Per space, unset maps add no constraint and the rest merge — parts of
        one device at different offsets fit together — but maps covering the
        same addresses differently conflict.

        Maps naming the same addresses in a different shape agree: ranges that
        touch describe one readable run, so the result is coalesced and a read
        may span it. A gap no map claims still separates two runs.

        Raises ``ValueError`` if the maps conflict; ``whose`` names whose maps
        are being merged in the error — a callable receives the conflicting
        space, so the message can say which one (``register_ranges`` alone is
        ambiguous between holding and input).
        """
        describe = whose if callable(whose) else lambda _space: whose
        by_space: dict[Space, set[tuple[Range, ...] | None]] = {}
        for device in maps:
            for space, ranges in device.maps.items():
                by_space.setdefault(space, set()).add(ranges)
        merged: dict[Space, tuple[Range, ...] | None] = {}
        for space, declared in by_space.items():
            constrained = {
                _coalesce(ranges) for ranges in declared if ranges is not None
            }
            if len(constrained) <= 1:
                merged[space] = next(iter(constrained), None)
                continue
            joint = tuple(sorted({r for ranges in constrained for r in ranges}))
            try:
                _validate_ranges(joint)  # overlap is a conflict; touching is not
            except ValueError as err:
                raise ValueError(
                    f"{describe(space)} must agree on {_RANGE_ATTR[space]} where "
                    f"their maps overlap, but got conflicting values: "
                    f"{sorted(constrained)}"
                ) from err
            merged[space] = _coalesce(joint)
        return cls(merged)
