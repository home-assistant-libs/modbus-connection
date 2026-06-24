"""The ``Component`` base class: a device sub-system of typed register fields."""

from __future__ import annotations

from collections.abc import Callable
from functools import cached_property
from typing import TYPE_CHECKING, Any

from ._planning import (
    CoilItem,
    Range,
    RegisterItem,
    _bulk_read_coils,
    _bulk_read_registers,
    _plan_blocks,
    _register_spans,
)
from .fields import CoilField, RegisterField

if TYPE_CHECKING:
    from .._protocol import ModbusUnit

UpdateListener = Callable[[], None]


class Component:
    """A device sub-system whose attributes map to registers and coils.

    Subclasses declare ``RegisterField`` / ``CoilField`` descriptors (usually via
    the typed factories). Each component reads only its own registers, so it can
    refresh independently; listeners registered via :meth:`add_update_listener`
    fire after each update (so one entity per component can subscribe).

    A device that pools several components into one update fetches them together;
    declare :attr:`register_ranges` / :attr:`coil_ranges` (e.g. from the device's
    datasheet) — as class attributes on a subclass or per instance — so reads
    never cross an unreadable gap.

    The read plan (which blocks to fetch) is derived from the static field layout
    and cached on first :meth:`async_update`, so each subsequent poll reuses it
    rather than re-planning. The fields and ``register_ranges`` / ``coil_ranges``
    are read once at that point; mutating them afterwards is not supported — set
    the ranges before the first update, and build a new component to change the
    field layout.
    """

    _register_fields: dict[str, RegisterField[Any]] = {}
    _coil_fields: dict[str, CoilField] = {}

    # The device's readable address ranges; None falls back to gap-based planning.
    # Override on a subclass (or set per instance) to constrain reads to the
    # addresses the device actually answers.
    register_ranges: tuple[Range, ...] | None = None
    coil_ranges: tuple[Range, ...] | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        registers: dict[str, RegisterField[Any]] = {}
        coils: dict[str, CoilField] = {}
        for klass in reversed(cls.__mro__):
            for name, value in vars(klass).items():
                if isinstance(value, RegisterField):
                    registers[name] = value
                elif isinstance(value, CoilField):
                    coils[name] = value
        cls._register_fields = registers
        cls._coil_fields = coils

    def __init__(self, unit: ModbusUnit, index: int = 1) -> None:
        self._unit = unit
        self._index = index
        self._values: dict[str, Any] = {}
        self._coils: dict[str, bool | None] = {}
        self._listeners: list[UpdateListener] = []

    def _address(self, field: RegisterField[Any] | CoilField) -> int:
        return field.address + field.stride * (self._index - 1)

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

    # -- update --------------------------------------------------------------

    @cached_property
    def register_items(self) -> list[RegisterItem]:
        """This component's register read targets, scale registers resolved.

        Derived once from the static field layout and cached for the instance's
        life; do not mutate the field set afterwards.
        """
        items = []
        for field in self._register_fields.values():
            scale_address = None
            if field.scale_register is not None:
                scale_address = field.scale_register + field.scale_register_stride * (
                    self._index - 1
                )
            items.append(
                RegisterItem(self._address(field), field, self._values, scale_address)
            )
        return items

    @cached_property
    def coil_items(self) -> list[CoilItem]:
        """This component's coil read targets (absolute address, field, store)."""
        return [(self._address(f), f, self._coils) for f in self._coil_fields.values()]

    @cached_property
    def _register_blocks(self) -> list[tuple[int, int]]:
        return _plan_blocks(_register_spans(self.register_items), self.register_ranges)

    @cached_property
    def _coil_blocks(self) -> list[tuple[int, int]]:
        spans = ((address, 1) for address, _, _ in self.coil_items)
        return _plan_blocks(spans, self.coil_ranges)

    def notify(self) -> None:
        """Fire every registered update listener."""
        for listener in list(self._listeners):
            listener()

    async def async_update(self) -> None:
        """Read this component's registers and coils, then notify listeners.

        Reads only this sub-system's own registers, so it can refresh on its own.
        A device that owns several components can instead pool their
        :attr:`register_items` / :attr:`coil_items` into one bulk read. The block
        plan is built on the first call and reused on later polls.
        """
        await _bulk_read_registers(
            self._unit, self.register_items, self._register_blocks
        )
        await _bulk_read_coils(self._unit, self.coil_items, self._coil_blocks)
        self.notify()

    # -- writes --------------------------------------------------------------

    async def write(self, field: str, value: Any) -> None:
        """Write a writable register or coil by attribute name.

        If the field has a ``level_coil`` (a write-unlock/override coil), it is
        first set to ``False`` so the device accepts the write.
        """
        if field in self._register_fields:
            register = self._register_fields[field]
            if not register.writable:
                raise AttributeError(f"{field} is read-only")
            await self._unlock(register)
            address = self._address(register)
            words = register.encode(value)
            if len(words) == 1:
                await self._unit.write_register(address, words[0])
            else:
                await self._unit.write_registers(address, words)
        elif field in self._coil_fields:
            coil_field = self._coil_fields[field]
            if not coil_field.writable:
                raise AttributeError(f"{field} is read-only")
            await self._unlock(coil_field)
            await self._unit.write_coil(self._address(coil_field), bool(value))
        else:
            raise AttributeError(f"unknown field {field!r}")

    async def _unlock(self, field: RegisterField[Any] | CoilField) -> None:
        """Release the field's write-unlock coil (set it to False)."""
        if field.level_coil is None:
            return
        address = field.level_coil + field.level_coil_stride * (self._index - 1)
        await self._unit.write_coil(address, False)
