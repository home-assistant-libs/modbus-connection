"""The ``ComponentGroup``: several components on one unit, refreshed together."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from ._planning import (
    _MAX_GAP,
    _MAX_SPAN,
    Range,
    Raw,
    ReadPlan,
    Space,
    _merge_raw,
    _Readable,
)

if TYPE_CHECKING:
    from .._protocol import ModbusUnit
    from .component import Component


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
        """The readable ranges per space; components sharing a space must agree."""
        by_space: dict[Space, list[Component]] = {}
        for component in self._components:
            by_space.setdefault(component.register_space, []).append(component)
        ranges: dict[Space, tuple[Range, ...] | None] = {}
        for space, components in by_space.items():
            distinct = {c.register_ranges for c in components}
            if len(distinct) > 1:
                raise ValueError(
                    f"every {space}-space component in a ComponentGroup must share "
                    f"register_ranges, but got differing values: {distinct}"
                )
            ranges[space] = next(iter(distinct), None)
        ranges["coil"] = self._shared("coil_ranges", None)
        ranges["discrete"] = self._shared("discrete_ranges", None)
        return ranges

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
