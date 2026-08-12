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


def _coalesce(ranges: tuple[Range, ...], *, within: int = 0) -> tuple[Range, ...]:
    """Join ranges that touch or overlap, or sit within ``within`` of each other.

    Two ranges with nothing between them describe one readable run, so a read
    may span both. Only called on maps whose parts have already been checked
    for conflicts.
    """
    joined: list[Range] = []
    for low, high in sorted(ranges):
        if joined and low <= joined[-1][1] + 1 + within:
            joined[-1] = (joined[-1][0], max(joined[-1][1], high))
        else:
            joined.append((low, high))
    return tuple(joined)


def _partitioned(
    maps: Iterable[tuple[Range, ...]], cutters: Iterable[tuple[Range, ...]]
) -> tuple[Range, ...]:
    """The addresses ``maps`` cover, split where any map in ``cutters`` starts or stops.

    A map that splits one run of addresses into parts says a read may not cross
    where it splits them, so a merge keeps those splits. Addresses something
    merely *reads* cut nothing — they are covered, not partitioned.
    """
    cuts = sorted(
        {edge for ranges in cutters for low, high in ranges for edge in (low, high + 1)}
    )
    partitioned: list[Range] = []
    for low, high in _coalesce(tuple(r for ranges in maps for r in ranges)):
        start = low
        for cut in cuts:
            if start < cut <= high:
                partitioned.append((start, cut - 1))
                start = cut
        partitioned.append((start, high))
    return tuple(partitioned)


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
    # Spaces whose ranges are a claim — evidence of what something reads, with
    # dropped addresses cut out — rather than an exhaustive declaration. Two
    # claims may overlap; two declarations that overlap contradict each other.
    claimed: frozenset[Space] = frozenset()

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
        return DeviceRanges(shifted, claimed=self.claimed)

    def widened(self, claims: Mapping[Space, tuple[Range, ...]]) -> DeviceRanges:
        """Return these maps with ``claims`` folded into the spaces they cover.

        A claim says only that something reads those addresses, not that the
        device serves the span they sit in, so it is added to a map rather
        than checked against it. Boundaries the map already draws are kept.
        """
        if not claims:
            return self
        widened: dict[Space, tuple[Range, ...] | None] = dict(self.maps)
        for space, ranges in claims.items():
            existing = (tuple(widened.get(space) or ()),)
            widened[space] = _partitioned((*existing, ranges), existing)
        return DeviceRanges(widened, claimed=self.claimed)

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

        Maps naming the same addresses in a different shape agree — they are
        compared by the addresses they name. The merged map keeps every
        boundary any of them draws, so pooling never widens a read past a
        split a component declared, and a gap no map claims still separates
        two runs.

        Only declarations can conflict, and only declarations draw
        boundaries. A claimed map (``claimed``) is evidence of reads: it
        overlaps freely, its coverage widens the merge, and touching claims
        describe one run — a hole splits by not being covered.

        Raises ``ValueError`` if the maps conflict; ``whose`` names whose maps
        are being merged in the error — a callable receives the conflicting
        space, so the message can say which one (``register_ranges`` alone is
        ambiguous between holding and input).
        """
        describe = whose if callable(whose) else lambda _space: whose
        declared_by: dict[Space, set[tuple[Range, ...]]] = {}
        claimed_by: dict[Space, set[tuple[Range, ...]]] = {}
        for device in maps:
            for space, ranges in device.maps.items():
                if ranges is None:
                    continue
                kind = claimed_by if space in device.claimed else declared_by
                kind.setdefault(space, set()).add(ranges)
        merged: dict[Space, tuple[Range, ...] | None] = {}
        for space in declared_by.keys() | claimed_by.keys():
            declared = declared_by.get(space, set())
            if declared:
                joint = tuple(
                    sorted({r for ranges in declared for r in _coalesce(ranges)})
                )
                try:
                    _validate_ranges(joint)  # overlap is a conflict; touching is not
                except ValueError as err:
                    raise ValueError(
                        f"{describe(space)} must agree on {_RANGE_ATTR[space]} "
                        f"where their maps overlap, but got conflicting values: "
                        f"{sorted(declared)}"
                    ) from err
            parts = declared | claimed_by.get(space, set())
            merged[space] = _partitioned(parts, declared)
        return cls(merged, claimed=frozenset(claimed_by.keys() - declared_by.keys()))
