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
    """Several :class:`Component`s on one unit, refreshed in pooled block reads.

    Groups the sub-systems of one physical device — e.g. a Trovis controller's
    water heater and heating circuits 1-3 — and reads them together: their
    register and coil targets are merged into a single consolidated set of block
    reads, so adjacent registers from different components are fetched in the same
    Modbus call rather than each component querying on its own. Each component's
    listeners fire after the update.

    Components may mix register spaces — an input (FC04) sub-system and a holding
    (FC03) sub-system in the same group are read with separate block reads — and
    likewise mix bit spaces, coils (FC01) and discrete inputs (FC02).

    The pooled plan is built from the components' static layout on the first
    :meth:`async_update` and reused on every later poll. The readable address
    ``ranges`` come from the components — they describe one device's address map.
    ``register_ranges`` applies per register space, so components sharing a space
    must declare the same :attr:`Component.register_ranges` (a device's input and
    holding ranges may differ); every component must share
    :attr:`Component.coil_ranges`, :attr:`Component.discrete_ranges`,
    :attr:`Component.max_gap` and :attr:`Component.max_span` (they describe one
    device). A mismatch raises ``ValueError``.

    The component list, their fields, and the ranges are read once and cached;
    mutating any of them after the first update is not supported — build a new
    ``ComponentGroup`` instead.
    """

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
        """Refresh every component in one pooled set of reads, then notify each.

        The block plan is built on the first call and reused on later polls. Pass
        ``notify=False`` to read without firing the components' listeners, for a
        caller that notifies them itself.

        The pooled read fetches each member's own fields plus any
        :func:`repeating_group` counts; a second pass then sizes and reads each
        member's register-count groups, so those refresh inside the group too.

        Like :meth:`Component.async_update`, a block answering with a Modbus
        exception response raises
        :class:`~modbus_connection.exceptions.BlockReadError` and fails the whole
        update — here that is any block across the pooled members.
        """
        await self._refresh(collect_raw=False, notify=notify)
