"""Shared ``repeating_group`` machinery for ``Component`` and ``ManualComponent``.

Not part of the public API — mixed into the two component classes, which supply
the group classifications and read the folded targets.
"""

from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING, Any

from ._planning import RegisterItem
from .component_group import ComponentGroup

if TYPE_CHECKING:
    from .._protocol import ModbusUnit
    from ._planning import BitItem, RegisterSpace
    from .component import Component, RepeatingGroupField


class _RepeatingGroups:
    """``repeating_group`` state and update, shared by the two component classes.

    The host supplies ``_unit`` and, split by count kind, ``_static_groups``
    (fixed ``int``) and ``_repeating_fields`` (``RegisterField``). It calls
    :meth:`_build_groups` once to set up per-instance state (and build the static
    instances), folds :attr:`_count_items` and the fixed-count instances' items
    into its read plan, and awaits :meth:`async_update_repeating_groups` as the second
    pass.
    """

    _unit: ModbusUnit
    _base_offset: int = 0
    _count_space: RegisterSpace = "holding"
    _static_groups: dict[str, RepeatingGroupField[Any]] = {}
    _repeating_fields: dict[str, RepeatingGroupField[Any]] = {}

    def _build_groups(self) -> None:
        """Initialise group state and build the fixed-count (static) instances."""
        self._groups: dict[str, list[Component]] = {}
        self._counts: dict[str, int | None] = {}
        self._instance_group: ComponentGroup | None = None
        for name, field in self._static_groups.items():
            self._groups[name] = self._build_instances(field, 0, field.count)

    def _build_instances(
        self, field: RepeatingGroupField[Any], start: int, stop: int
    ) -> list[Component]:
        return [
            field.component_class(
                self._unit, base_offset=self._base_offset + i * field.stride
            )
            for i in range(start, stop)
        ]

    @cached_property
    def _count_items(self) -> list[RegisterItem]:
        """Read targets for each register-count group's count register."""
        items = []
        for name, field in self._repeating_fields.items():
            count_field = field.count
            count_field.name = name  # the decoded count lands in ``_counts[name]``
            items.append(
                RegisterItem(
                    count_field.address + self._base_offset,
                    count_field,
                    self._counts,
                    None,
                    self._count_space,
                )
            )
        return items

    @cached_property
    def _static_register_items(self) -> list[RegisterItem]:
        """Register read targets of every fixed-count group's instances."""
        return [
            item
            for name in self._static_groups
            for instance in self._groups[name]
            for item in instance.register_items
        ]

    @cached_property
    def _static_bit_items(self) -> list[BitItem]:
        """Bit read targets of every fixed-count group's instances."""
        return [
            item
            for name in self._static_groups
            for instance in self._groups[name]
            for item in instance.bit_items
        ]

    def _invalidate_group_cache(self) -> None:
        """Drop the cached group read targets after group membership changes."""
        for attr in ("_count_items", "_static_register_items", "_static_bit_items"):
            self.__dict__.pop(attr, None)

    async def async_update_repeating_groups(self) -> None:
        """Size each register-count group to the count just read, and read them.

        The counts are already in ``self._counts`` (they are part of the read
        plan's ``_count_items``), so this is the second pass of an update. Reads
        the instances pooled among themselves, without notifying — the caller
        does. A :class:`ComponentGroup` calls this on each member after its pooled
        read, so a member's register-count groups refresh inside the group too.
        """
        if not self._repeating_fields:
            return
        instances: list[Component] = []
        for name, field in self._repeating_fields.items():
            value = self._counts.get(name)
            count = max(0, int(value)) if value is not None else 0
            existing = self._groups.get(name, [])
            if len(existing) != count:
                existing = existing[:count] + self._build_instances(
                    field, len(existing), count
                )
                self._groups[name] = existing
                self._instance_group = None
            instances.extend(existing)
        if instances:
            if self._instance_group is None:
                self._instance_group = ComponentGroup(self._unit, instances)
            await self._instance_group.async_update(notify=False)
