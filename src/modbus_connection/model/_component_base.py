"""Share component update and repeating-group behavior."""

from __future__ import annotations

from collections.abc import Callable
from functools import cached_property
from typing import TYPE_CHECKING, Any, cast

from ._const import Raw, RegisterSpace, Space
from ._planning import (
    ReadItem,
    ReadPlan,
    ResolvedField,
    _merge_raw,
    _Readable,
    undeclared_claims,
)
from ._ranges import DeviceRanges, _coalesce
from .fields import CoilField, DiscreteInputField, RegisterField, _BitField

if TYPE_CHECKING:
    from .component import Component, RepeatingGroupField

UpdateListener = Callable[[], None]


class _ComponentBase(_Readable):
    """Share update listeners and repeating groups between component types."""

    _base_offset: int = 0
    _instance_offset: int = 0
    _static_groups: dict[str, RepeatingGroupField[Any]] = {}
    _repeating_fields: dict[str, RepeatingGroupField[Any]] = {}
    # Provided by the concrete component types.
    max_gap: int
    max_span: int

    # -- listeners -----------------------------------------------------------

    def add_update_listener(self, listener: UpdateListener) -> Callable[[], None]:
        """Register a callback fired after each update; returns an unsubscribe."""
        self._listeners.append(listener)

        def remove() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return remove

    def notify(self) -> None:
        """Fire this component's update listeners, and each sub-instance's."""
        for group in self._groups.values():
            for instance in group:
                instance.notify()
        for listener in list(self._listeners):
            listener()

    # -- repeating groups ----------------------------------------------------

    @property
    def _count_space(self) -> RegisterSpace:
        # the register space a group's count register is read from; Component
        # overrides this to track its own ``register_space``.
        return "holding"

    def _build_groups(self) -> None:
        """Initialise listener and group state; build the fixed-count instances."""
        self._listeners: list[UpdateListener] = []
        self._groups: dict[str, list[Component]] = {}
        self._counts: dict[str, int | None] = {}
        for name, field in self._static_groups.items():
            # a static group's count is a fixed int (Component splits the two kinds)
            count = cast("int", field.count)
            self._groups[name] = self._build_instances(field, 0, count)

    def _build_instances(
        self, field: RepeatingGroupField[Any], start: int, stop: int
    ) -> list[Component]:
        # instances inherit the parent's block position (base_offset, which
        # also moves scale registers); their own per-instance shift applies to
        # fields only, so shared scale factors stay in the parent's fixed block —
        # unless the sub-unit sets ``scale_in_block``, which moves each instance's
        # scale registers with its shift too (a block carrying its own factors)
        instances = [
            field.component_class(
                self._unit,
                base_offset=self._base_offset,
                _instance_offset=self._instance_offset + i * field.stride,
            )
            for i in range(start, stop)
        ]
        for instance in instances:
            instance._parent = self
        return instances

    def _address(self, field: RegisterField[Any] | _BitField) -> int:
        """Where a field of this component's own block is read."""
        return field.address + self._base_offset + self._instance_offset

    def _scale_address(self, field: RegisterField[Any]) -> int:
        """Where a field's scale register is read."""
        assert field.scale_register is not None
        return field.scale_register

    def _resolve(
        self,
        field: RegisterField[Any] | CoilField | DiscreteInputField,
        space: Space,
    ) -> ResolvedField:
        """Resolve one of this component's fields to a read target."""
        scale_address = (
            self._scale_address(field)
            if isinstance(field, RegisterField) and field.scale_register is not None
            else None
        )
        return ResolvedField(
            field, self._address(field), field.count, scale_address, space
        )

    @cached_property
    def _own_items(self) -> list[ReadItem]:
        """This component's own read targets; provided by the concrete types."""
        raise NotImplementedError

    @cached_property
    def _count_items(self) -> list[ReadItem]:
        """Read targets for each register-count group's count register."""
        return [
            # a register-count group's count is a RegisterField, named for its
            # group at registration so the decoded count lands in ``_counts``
            ReadItem(
                self._resolve(
                    cast("RegisterField[Any]", field.count), self._count_space
                ),
                self._counts,
            )
            for field in self._repeating_fields.values()
        ]

    @cached_property
    def _static_items(self) -> list[ReadItem]:
        """Read targets of every fixed-count group's instances."""
        return [
            item
            for name in self._static_groups
            for instance in self._groups[name]
            for item in instance._read_items
        ]

    @cached_property
    def _read_items(self) -> list[ReadItem]:
        """This component's read targets: its own fields and every instance's."""
        return self._own_items + self._static_items + self._dynamic_items

    @cached_property
    def _dynamic_items(self) -> list[ReadItem]:
        """Read targets of every register-count group's current instances."""
        return [
            item
            for name in self._repeating_fields
            for instance in self._groups.get(name, [])
            for item in instance._read_items
        ]

    def _instances(self) -> list[Component]:
        """Every repeating-group instance, fixed-count and register-count."""
        return [instance for group in self._groups.values() for instance in group]

    def _resolved_ranges(self) -> DeviceRanges:
        """This object's readable map; provided by the concrete types."""
        raise NotImplementedError

    def _with_instance_ranges(self, own: DeviceRanges) -> DeviceRanges:
        """Merge every repeating-group instance's readable ranges into ``own``.

        A ``repeating_group``'s instances are read from this component's own
        plan, so their declared maps only mean something if they reach it.

        Where the merged map constrains a space, a part that declares nothing
        for it stands for the addresses it reads by itself — like an undeclared
        member of a ``ComponentGroup``.

        Raises ``ValueError`` if the maps conflict.
        """
        instances = self._instances()
        if not instances:
            return own
        try:
            merged = DeviceRanges.merged(
                # an instance's own repeating groups are merged into its map
                [own, *(instance._resolved_ranges() for instance in instances)],
                whose="a component and its repeating_group instances",
            )
        except ValueError as err:
            # The usual cause is a sub-component that declares the ranges its
            # parent already covers, so name the fix rather than only the clash.
            raise ValueError(
                f"{err}. A repeating_group's instances are read from "
                f"{type(self).__name__}'s own plan, so the sub-component normally "
                f"leaves its readable ranges unset and lets the parent's map cover "
                f"the repeated addresses"
            ) from err
        claims = undeclared_claims(
            [
                (own, self._own_items, self.max_gap, self.max_span),
                *(
                    (i._resolved_ranges(), i._read_items, i.max_gap, i.max_span)
                    for i in instances
                ),
            ]
        )
        # The parts are all inside this one component, so their claims join
        # across max_gap and a read may bridge between instances.
        return merged.widened(
            {
                s: _coalesce(r, within=self.max_gap)
                for s, r in claims.items()
                if merged.for_space(s) is not None
            }
        )

    def _invalidate_caches(self) -> None:
        # Owns the group read-target caches; the plan is the base's.
        for attr in (
            "_read_items",
            "_own_items",
            "_count_items",
            "_static_items",
            "_dynamic_items",
        ):
            self.__dict__.pop(attr, None)
        super()._invalidate_caches()

    async def async_update_repeating_groups(self) -> None:
        """Resize register-count groups and read any instances just added."""
        await self._refresh_repeating_groups(collect_raw=False)

    async def _refresh_repeating_groups(self, *, collect_raw: bool) -> Raw:
        """Resize register-count groups to the counts just read.

        The instances are part of this component's own plan, so a poll at the
        current size has already read them. A resize invalidates that plan for
        the next poll; instances the resize *added* are read here, so the
        update that grew a group returns it complete.
        """
        raw: Raw = {}
        for name in self._static_groups:
            for instance in self._groups[name]:
                _merge_raw(
                    raw,
                    await instance._refresh_repeating_groups(collect_raw=collect_raw),
                )
        if not self._repeating_fields:
            return raw
        added: list[Component] = []
        resized = False
        for name, field in self._repeating_fields.items():
            value = self._counts.get(name)
            count = max(0, int(value)) if value is not None else 0
            existing = self._groups.get(name, [])
            if len(existing) == count:
                continue
            resized = True
            new = self._build_instances(field, len(existing), count)
            self._groups[name] = existing[:count] + new
            added.extend(new)
        if resized:
            self._invalidate_caches()
        if added:
            plan = ReadPlan.build(
                [item for instance in added for item in instance._read_items],
                self._resolved_ranges(),
                max_gap=self.max_gap,
                max_span=self.max_span,
            )
            _merge_raw(raw, await plan.execute(self._unit, collect_raw=collect_raw))
        # An instance's own register-count groups resize from the counts the
        # reads above decoded.
        for name in self._repeating_fields:
            for instance in self._groups.get(name, []):
                _merge_raw(
                    raw,
                    await instance._refresh_repeating_groups(collect_raw=collect_raw),
                )
        return raw
