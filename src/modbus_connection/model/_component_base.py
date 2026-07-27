"""Share component update and repeating-group behavior."""

from __future__ import annotations

from collections.abc import Callable
from functools import cached_property
from typing import TYPE_CHECKING, Any, cast

from ._const import Range, Raw, RegisterSpace, Space
from ._planning import ReadItem, _merge_raw, _Readable
from ._ranges import _merge_range_maps
from .component_group import ComponentGroup

if TYPE_CHECKING:
    from .component import Component, RepeatingGroupField
    from .fields import RegisterField

UpdateListener = Callable[[], None]


class _ComponentBase(_Readable):
    """Share update listeners and repeating groups between component types."""

    _base_offset: int = 0
    _instance_offset: int = 0
    _static_groups: dict[str, RepeatingGroupField[Any]] = {}
    _repeating_fields: dict[str, RepeatingGroupField[Any]] = {}

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
        self._instance_group: ComponentGroup | None = None
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
        return [
            field.component_class(
                self._unit,
                base_offset=self._base_offset,
                _instance_offset=self._instance_offset + i * field.stride,
            )
            for i in range(start, stop)
        ]

    @cached_property
    def _count_items(self) -> list[ReadItem]:
        """Read targets for each register-count group's count register."""
        items = []
        for name, field in self._repeating_fields.items():
            # a register-count group's count is a RegisterField (see the split above)
            count_field = cast("RegisterField[int]", field.count)
            count_field.name = name  # the decoded count lands in ``_counts[name]``
            items.append(
                ReadItem(
                    count_field.address + self._base_offset + self._instance_offset,
                    count_field,
                    self._counts,
                    self._count_space,
                )
            )
        return items

    @cached_property
    def _static_items(self) -> list[ReadItem]:
        """Read targets of every fixed-count group's instances."""
        return [
            item
            for name in self._static_groups
            for instance in self._groups[name]
            for item in instance._read_items
        ]

    def _with_static_ranges(
        self, own: dict[Space, tuple[Range, ...] | None]
    ) -> dict[Space, tuple[Range, ...] | None]:
        """Merge every fixed-count instance's readable ranges into ``own``.

        A fixed-count group's instances are read from this component's own plan
        (see ``_static_items``), so their declared maps only mean something if
        they reach it. A register-count group gets this from the ``ComponentGroup``
        its instances are pooled in, which merges its members' maps the same way.

        Raises ``ValueError`` if the maps conflict.
        """
        if not self._static_groups:
            return own
        by_space: dict[Space, list[tuple[Range, ...] | None]] = {
            space: [ranges] for space, ranges in own.items()
        }
        for name in self._static_groups:
            for instance in self._groups[name]:
                # an instance's own fixed-count groups are merged into its map
                for space, ranges in instance._resolved_ranges().items():
                    by_space.setdefault(space, []).append(ranges)
        return {
            space: _merge_range_maps(
                space, declared, whose="a component and its fixed-count instances"
            )
            for space, declared in by_space.items()
        }

    def _invalidate_caches(self) -> None:
        # Owns the group read-target caches; the plan is the base's.
        for attr in ("_count_items", "_static_items"):
            self.__dict__.pop(attr, None)
        super()._invalidate_caches()

    async def async_update_repeating_groups(self) -> None:
        """Resize and update register-count groups."""
        await self._refresh_repeating_groups(collect_raw=False)

    async def _refresh_repeating_groups(self, *, collect_raw: bool) -> Raw:
        """Refresh register-count groups and optionally collect raw values."""
        raw: Raw = {}
        for name in self._static_groups:
            for instance in self._groups[name]:
                _merge_raw(
                    raw,
                    await instance._refresh_repeating_groups(collect_raw=collect_raw),
                )
        if not self._repeating_fields:
            return raw
        # Size each register-count group to the count just read, growing or
        # trimming its instances and dropping the cached pooled group on a change.
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
            _merge_raw(
                raw,
                await self._instance_group._refresh(
                    collect_raw=collect_raw, notify=False
                ),
            )
        return raw
