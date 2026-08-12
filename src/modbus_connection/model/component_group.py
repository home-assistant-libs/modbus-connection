"""The ``ComponentGroup``: several components on one unit, refreshed together."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from ._const import _MAX_GAP, _MAX_SPAN, Raw
from ._planning import ReadPlan, _merge_raw, _Readable, undeclared_claims
from ._ranges import DeviceRanges, _coalesce

if TYPE_CHECKING:
    from .._protocol import ModbusUnit
    from .component import Component
    from .manual import ManualComponent


class ComponentGroup(_Readable):
    """Pool reads for several components on one unit."""

    def __init__(
        self,
        unit: ModbusUnit,
        components: Iterable[Component | ManualComponent],
        *,
        _one_run: bool = False,
    ) -> None:
        self._unit = unit
        self._components = list(components)
        self._one_run = _one_run
        for component in self._components:
            component._parent = self
        self._ranges = self._ranges_by_space()
        self._max_span: int = self._shared("max_span", _MAX_SPAN)

    def _invalidate_caches(self) -> None:
        self._ranges = self._ranges_by_space()
        super()._invalidate_caches()

    def _ranges_by_space(self) -> DeviceRanges:
        """The readable ranges per space, merged over the member components.

        Members describe one device, so their maps must fit together. A member
        that declares nothing for a space stands for the addresses it reads by
        itself, which keeps a pooled read from bridging into addresses no
        member claims.

        A ``repeating_group``'s instances are the exception (``_one_run``):
        they tile one run of their parent component, so their claims join
        across ``max_gap`` and a pooled read may bridge between them. A map an
        instance does declare — ``restrict_fields`` synthesises one — still
        bounds the read.

        Raises ``ValueError`` if the maps conflict.
        """
        claims = undeclared_claims(
            (c._resolved_ranges(), c._read_items, c.max_gap, c.max_span)
            for c in self._components
        )
        if self._one_run:
            within = self._shared("max_gap", _MAX_GAP)
            claims = {
                space: _coalesce(ranges, within=within)
                for space, ranges in claims.items()
            }
        return DeviceRanges.merged(
            [component._resolved_ranges() for component in self._components],
            whose=lambda space: f"every {space}-space component in a ComponentGroup",
        ).widened(claims)

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
            # Every space a member reads carries a map here — its own if it
            # declared none — so gap bridging has nothing left to decide.
            max_gap=0,
            max_span=self._max_span,
        )

    def notify(self) -> None:
        """Fire each member component's update listeners."""
        for component in self._components:
            component.notify()

    def _verify_read(self) -> None:
        """Run each member's post-read check before any member notifies."""
        for component in self._components:
            component._verify_read()

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
