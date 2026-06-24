"""The ``ComponentGroup``: several components on one unit, refreshed together."""

from __future__ import annotations

from collections.abc import Iterable
from functools import cached_property
from typing import TYPE_CHECKING

from ._planning import (
    CoilItem,
    Range,
    RegisterItem,
    _bulk_read_coils,
    _bulk_read_registers,
    _plan_blocks,
    _register_spans,
)
from .component import Component

if TYPE_CHECKING:
    from .._protocol import ModbusUnit


class ComponentGroup:
    """Several :class:`Component`s on one unit, refreshed in pooled block reads.

    Groups the sub-systems of one physical device — e.g. a Trovis controller's
    water heater and heating circuits 1-3 — and reads them together: their
    register and coil targets are merged into a single consolidated set of block
    reads, so adjacent registers from different components are fetched in the same
    Modbus call rather than each component querying on its own. Each component's
    listeners fire after the update.

    The pooled plan is built from the components' static layout on the first
    :meth:`async_update` and reused on every later poll. The readable address
    ``ranges`` come from the components — they describe one device's address map,
    so every component in the group must declare the same
    :attr:`Component.register_ranges` / :attr:`Component.coil_ranges`; a mismatch
    raises ``ValueError``.

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
        self._register_ranges = self._shared_ranges("register_ranges")
        self._coil_ranges = self._shared_ranges("coil_ranges")

    def _shared_ranges(self, attr: str) -> tuple[Range, ...] | None:
        """The ranges shared by every component, or raise if they disagree."""
        distinct = {getattr(c, attr) for c in self._components}
        if len(distinct) > 1:
            raise ValueError(
                f"every component in a ComponentGroup must share {attr}, "
                f"but got differing values: {distinct}"
            )
        return next(iter(distinct), None)

    @cached_property
    def _register_items(self) -> list[RegisterItem]:
        return [item for c in self._components for item in c.register_items]

    @cached_property
    def _coil_items(self) -> list[CoilItem]:
        return [item for c in self._components for item in c.coil_items]

    @cached_property
    def _register_blocks(self) -> list[tuple[int, int]]:
        return _plan_blocks(
            _register_spans(self._register_items), self._register_ranges
        )

    @cached_property
    def _coil_blocks(self) -> list[tuple[int, int]]:
        spans = ((address, 1) for address, _, _ in self._coil_items)
        return _plan_blocks(spans, self._coil_ranges)

    async def async_update(self) -> None:
        """Refresh every component in one pooled set of reads, then notify each.

        The block plan is built on the first call and reused on later polls.
        """
        await _bulk_read_registers(
            self._unit, self._register_items, self._register_blocks
        )
        await _bulk_read_coils(self._unit, self._coil_items, self._coil_blocks)
        for component in self._components:
            component.notify()
