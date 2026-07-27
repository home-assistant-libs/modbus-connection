"""The ``ComponentGroup``: several components on one unit, refreshed together."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, cast

from ._planning import (
    _MAX_GAP,
    _MAX_SPAN,
    Range,
    Raw,
    ReadPlan,
    Space,
    _merge_raw,
    _Readable,
    _validate_ranges,
)

if TYPE_CHECKING:
    from .._protocol import ModbusUnit
    from .component import Component


# The attribute a space's readable ranges are declared under, for error messages.
_RANGE_ATTR: dict[Space, str] = {
    "holding": "register_ranges",
    "input": "register_ranges",
    "coil": "coil_ranges",
    "discrete": "discrete_ranges",
}


def _merge_ranges(
    space: Space, declared: list[tuple[Range, ...] | None]
) -> tuple[Range, ...] | None:
    """Merge one space's readable ranges over a group's components.

    Members describe one device, so their maps must fit together: components at
    different offsets contribute different parts of it and are merged, but two
    that overlap without agreeing describe the device differently, and a member
    that constrains a space cannot be pooled with one that leaves it open.

    Raises ``ValueError`` if the maps conflict.
    """
    attr = _RANGE_ATTR[space]
    distinct = set(declared)
    if len(distinct) == 1:
        return next(iter(distinct))
    if None in distinct:
        raise ValueError(
            f"every {space}-space component in a ComponentGroup must declare "
            f"{attr} if any does, but some left it unset"
        )
    constrained = cast("set[tuple[Range, ...]]", distinct)  # no None left
    merged = tuple(sorted({r for ranges in constrained for r in ranges}))
    try:
        _validate_ranges(merged)
    except ValueError as err:
        raise ValueError(
            f"every {space}-space component in a ComponentGroup must agree on "
            f"{attr} where their maps overlap, but got conflicting values: "
            f"{sorted(constrained)}"
        ) from err
    return merged


class ComponentGroup(_Readable):
    """Pool reads for several components on one unit."""

    def __init__(
        self,
        unit: ModbusUnit,
        components: Iterable[Component],
    ) -> None:
        self._unit = unit
        self._components = list(components)
        self._ranges = self._ranges_by_space()
        self._max_gap: int = self._shared("max_gap", _MAX_GAP)
        self._max_span: int = self._shared("max_span", _MAX_SPAN)

    def _ranges_by_space(self) -> dict[Space, tuple[Range, ...] | None]:
        """The readable ranges per space, merged over the member components."""
        by_space: dict[Space, list[tuple[Range, ...] | None]] = {}
        for component in self._components:
            for space, ranges in component._resolved_ranges().items():
                by_space.setdefault(space, []).append(ranges)
        return {
            space: _merge_ranges(space, declared)
            for space, declared in by_space.items()
        }

    def _shared[V](self, attr: str, default: V) -> V:
        """The value of ``attr`` shared by every component, or raise if they differ."""
        distinct = {getattr(c, attr) for c in self._components}
        if len(distinct) > 1:
            raise ValueError(
                f"every component in a ComponentGroup must share {attr}, "
                f"but got differing values: {distinct}"
            )
        return next(iter(distinct), default)

    def _build_plan(self) -> ReadPlan:
        return ReadPlan.build(
            [item for c in self._components for item in c._read_items],
            self._ranges,
            max_gap=self._max_gap,
            max_span=self._max_span,
        )

    def notify(self) -> None:
        """Fire each member component's update listeners."""
        for component in self._components:
            component.notify()

    async def _refresh_repeating_groups(self, *, collect_raw: bool) -> Raw:
        # the pooled first pass read each member's count registers; now drive
        # each member's own second pass so their register-count groups refresh
        raw: Raw = {}
        for component in self._components:
            _merge_raw(
                raw, await component._refresh_repeating_groups(collect_raw=collect_raw)
            )
        return raw

    async def async_update(self, *, notify: bool = True) -> None:
        """Refresh every component with pooled reads.

        Raises ``BlockReadError`` if the device rejects a block.
        """
        await self._refresh(collect_raw=False, notify=notify)
