"""The ``Component`` base class: a device sub-system of typed register fields."""

from __future__ import annotations

from collections.abc import Callable
from functools import cached_property
from typing import TYPE_CHECKING, Any, overload

from ._planning import (
    _MAX_GAP,
    _MAX_SPAN,
    BitItem,
    BitSpace,
    Range,
    RegisterItem,
    RegisterSpace,
    _bulk_read_bits,
    _bulk_read_registers,
    _plan_bit_blocks,
    _plan_register_blocks,
)
from ._writing import write_bit_field, write_register_field
from .component_group import ComponentGroup
from .fields import RegisterField, _BitField

if TYPE_CHECKING:
    from .._protocol import ModbusUnit

UpdateListener = Callable[[], None]


class Component:
    """A device sub-system whose attributes map to registers, coils and inputs.

    Subclasses declare ``RegisterField`` / ``CoilField`` / ``DiscreteInputField``
    descriptors (usually via the typed factories). Each component reads only its
    own registers, so it can refresh independently; listeners registered via
    :meth:`add_update_listener` fire after each update (so a consumer can subscribe
    per component).

    A device that pools several components into one update fetches them together;
    declare :attr:`register_ranges` / :attr:`coil_ranges` (e.g. from the device's
    datasheet) — as class attributes on a subclass or per instance — so reads
    never cross an unreadable gap.

    A component's register fields all live in one register space: holding (FC03,
    the default) or input (FC04). Set :attr:`register_space` to ``"input"`` for a
    read-only input-register sub-system. Input registers cannot be written. Bit
    fields carry their own space: ``coil`` (FC01, writable) and ``discrete_input``
    (FC02, read-only) may be mixed in one component and are read separately.

    For a device with repeated identical sub-units (e.g. heating circuits), model
    the sub-unit once and pass ``index`` (1-based) per instance; each field's
    ``stride`` is the address step between sub-units for that register, so the
    absolute address is ``field.address + field.stride * (index - 1)``. Fields
    often carry different strides, as devices group registers by type rather than
    by sub-unit.

    When instead *every* field of a repeated sub-unit shares one step — the common
    case for a self-contained, contiguous repeating block — pass ``base_offset``:
    it shifts every field and bit address by that fixed amount, so you model the
    block once at instance 0's addresses and read instance *i* with
    ``base_offset = i * block_len``. It composes additively with ``index`` /
    ``stride`` and applies to reads and writes alike. Scale-factor registers
    (``scale_register``) are **not** shifted — a SunSpec repeating block's scale
    factors live in the shared fixed block, so they keep their absolute address;
    a per-instance scale register stays governed by ``scale_register_stride``.

    The read plan (which blocks to fetch) is derived from the static field layout
    and cached on first :meth:`async_update`, so each subsequent poll reuses it
    rather than re-planning. The fields and ``register_ranges`` / ``coil_ranges``
    are read once at that point; mutating them afterwards is not supported — set
    the ranges before the first update, and build a new component to change the
    field layout.
    """

    _register_fields: dict[str, RegisterField[Any]] = {}
    _bit_fields: dict[str, _BitField] = {}
    # repeating_group fields, split by count kind: a fixed ``int`` count is static
    # (its instances fold into the normal read like ordinary fields), a
    # ``RegisterField`` count is read at poll time (the two-phase repeating path).
    _static_groups: dict[str, RepeatingGroupField[Any]] = {}
    _repeating_fields: dict[str, RepeatingGroupField[Any]] = {}

    # The device's readable address ranges; None falls back to gap-based planning.
    # Override on a subclass (or set per instance) to constrain reads to the
    # addresses the device actually answers. Each applies within its own address
    # space — ``register_ranges`` to this component's register space, ``coil_ranges``
    # to coils (FC01) and ``discrete_ranges`` to discrete inputs (FC02), which are
    # distinct spaces with their own readable maps.
    register_ranges: tuple[Range, ...] | None = None
    coil_ranges: tuple[Range, ...] | None = None
    discrete_ranges: tuple[Range, ...] | None = None

    # Block-planning limits, overridable per device. ``max_gap`` only applies to
    # gap-based planning (no ranges): spans within this many addresses merge into
    # one read — higher means fewer reads but more over-reading. ``max_span`` caps
    # a single block's width (125 is the Modbus per-request ceiling; lower it for
    # a gateway that caps reads shorter).
    max_gap: int = _MAX_GAP
    max_span: int = _MAX_SPAN

    # The register space this component's fields are read from (FC03 / FC04).
    register_space: RegisterSpace = "holding"

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        registers: dict[str, RegisterField[Any]] = {}
        bits: dict[str, _BitField] = {}
        static_groups: dict[str, RepeatingGroupField[Any]] = {}
        repeating: dict[str, RepeatingGroupField[Any]] = {}
        for klass in reversed(cls.__mro__):
            for name, value in vars(klass).items():
                if isinstance(value, RegisterField):
                    registers[name] = value
                elif isinstance(value, _BitField):
                    bits[name] = value
                elif isinstance(value, RepeatingGroupField):
                    target = (
                        static_groups if isinstance(value.count, int) else repeating
                    )
                    target[name] = value
        cls._register_fields = registers
        cls._bit_fields = bits
        cls._static_groups = static_groups
        cls._repeating_fields = repeating

    def __init__(
        self, unit: ModbusUnit, index: int = 1, *, base_offset: int = 0
    ) -> None:
        self._unit = unit
        self._index = index
        self._base_offset = base_offset
        self._values: dict[str, Any] = {}
        self._bits: dict[str, bool | None] = {}
        self._listeners: list[UpdateListener] = []
        # repeating_group state, keyed by group-field name: the live sub-component
        # instances per group, and the last-read count for register-count groups.
        self._groups: dict[str, list[Component]] = {}
        self._counts: dict[str, int | None] = {}
        # Fixed-count groups are static: build their instances now so they fold
        # into the normal read plan (register_items / bit_items, like ordinary
        # fields). Register-count groups are sized later, in async_update.
        for name, field in self._static_groups.items():
            self._groups[name] = [
                field.component_class(
                    self._unit, base_offset=base_offset + i * field.stride
                )
                for i in range(field.count)
            ]
        # The pooled reader for the register-count instances; rebuilt only when an
        # instance set changes, reused while the counts hold.
        self._instance_group: ComponentGroup | None = None

    def _address(self, field: RegisterField[Any] | _BitField) -> int:
        return field.address + field.stride * (self._index - 1) + self._base_offset

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
        life; do not mutate the field set afterwards. Includes each
        :func:`repeating_group` count register and, for fixed-count groups, their
        instances' registers — so the normal read fetches the counts and every
        static instance in one pass.
        """
        items = []
        for field in self._register_fields.values():
            scale_address = None
            if field.scale_register is not None:
                scale_address = field.scale_register + field.scale_register_stride * (
                    self._index - 1
                )
            items.append(
                RegisterItem(
                    self._address(field),
                    field,
                    self._values,
                    scale_address,
                    self.register_space,
                )
            )
        static = [
            item
            for name in self._static_groups
            for instance in self._groups[name]
            for item in instance.register_items
        ]
        return items + self._count_items + static

    @cached_property
    def bit_items(self) -> list[BitItem]:
        """This component's bit read targets (coils and discrete inputs).

        Includes the bits of any fixed-count :func:`repeating_group` instances, so
        they read in the normal pass alongside this component's own bits.
        """
        own = [(self._address(f), f, self._bits) for f in self._bit_fields.values()]
        static = [
            item
            for name in self._static_groups
            for instance in self._groups[name]
            for item in instance.bit_items
        ]
        return own + static

    @cached_property
    def _register_blocks(self) -> dict[RegisterSpace, list[tuple[int, int]]]:
        return _plan_register_blocks(
            self.register_items,
            {self.register_space: self.register_ranges},
            max_gap=self.max_gap,
            max_span=self.max_span,
        )

    @cached_property
    def _bit_blocks(self) -> dict[BitSpace, list[tuple[int, int]]]:
        ranges: dict[BitSpace, tuple[Range, ...] | None] = {
            "coil": self.coil_ranges,
            "discrete": self.discrete_ranges,
        }
        return _plan_bit_blocks(
            self.bit_items, ranges, max_gap=self.max_gap, max_span=self.max_span
        )

    def notify(self) -> None:
        """Fire this component's update listeners, and each sub-instance's.

        A :func:`repeating_group` instance is notified here too, so one update
        notifies the whole component — its own subscribers and every instance's.
        """
        for group in self._groups.values():
            for instance in group:
                instance.notify()
        for listener in list(self._listeners):
            listener()

    async def async_update(self) -> None:
        """Read this component's registers and coils, then notify listeners.

        Reads only this sub-system's own registers, so it can refresh on its own.
        A device that owns several components can instead pool their
        :attr:`register_items` / :attr:`bit_items` into one bulk read. The block
        plan is built on the first call and reused on later polls.

        A :func:`repeating_group` field needs a second pass: the first read
        fetches the count (it is part of :attr:`register_items`), then the
        sized-out instances are read, pooled among themselves into as few reads
        as possible.
        """
        await _bulk_read_registers(
            self._unit, self.register_items, self._register_blocks
        )
        await _bulk_read_bits(self._unit, self.bit_items, self._bit_blocks)
        if not self._repeating_fields:
            self.notify()
            return

        # Size each group to the count just read, keeping survivors
        instances: list[Component] = []
        for name, field in self._repeating_fields.items():
            value = self._counts.get(name)
            count = max(0, int(value)) if value is not None else 0
            existing = self._groups.get(name, [])
            if len(existing) != count:
                existing = existing[:count] + [
                    field.component_class(
                        self._unit, base_offset=self._base_offset + i * field.stride
                    )
                    for i in range(len(existing), count)
                ]
                self._groups[name] = existing
                self._instance_group = None
            instances.extend(existing)

        # Read the instances without notifying — self.notify() below fires every
        # instance in one place.
        if instances:
            if self._instance_group is None:
                self._instance_group = ComponentGroup(self._unit, instances)
            await self._instance_group.async_update(notify=False)
        self.notify()

    @cached_property
    def _count_items(self) -> list[RegisterItem]:
        """Read targets for each repeating group's count register."""
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
                    self.register_space,
                )
            )
        return items

    # -- writes --------------------------------------------------------------

    async def write(self, field: str, value: Any) -> None:
        """Write a writable register or coil by attribute name.

        A field declared with a :data:`~modbus_connection.model.fields.WriteValidator`
        callable for ``writable`` has that validator run against ``value`` first; it
        returns the value to actually write (vetted or coerced), or raises to reject
        it before anything is sent to the device.

        A register field is written with FC06 (single) for a one-word value or
        FC16 (multiple) for a wider one, unless the field sets ``force_fc16``,
        which uses FC16 even for a single register (for a device that honours only
        FC16). Input registers (FC04) and discrete inputs (FC02) are physically
        read-only, so writing one raises ``AttributeError``. Override :meth:`write`
        in a subclass for any device-specific write sequencing.
        """
        if field in self._register_fields:
            register = self._register_fields[field]
            await write_register_field(
                self._unit,
                register,
                self._address(register),
                self.register_space,
                value,
                label=field,
            )
        elif field in self._bit_fields:
            bit_field = self._bit_fields[field]
            await write_bit_field(
                self._unit, bit_field, self._address(bit_field), value, label=field
            )
        else:
            raise AttributeError(f"unknown field {field!r}")


class RepeatingGroupField[C: Component]:
    """A list of sub-component instances whose length is read at poll time.

    Built by :func:`repeating_group`. Placed as a descriptor on a parent
    ``Component``; reading the attribute returns the ``list`` of instances from
    the last update (empty before the first), each a fully typed ``C``.
    """

    name: str = ""  # set by __set_name__ when used as a class descriptor

    def __init__(
        self, count: RegisterField[int] | int, component_class: type[C], *, stride: int
    ) -> None:
        self.count = count
        self.component_class = component_class
        self.stride = stride

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    if TYPE_CHECKING:

        @overload
        def __get__(self, obj: None, objtype: Any = ...) -> RepeatingGroupField[C]: ...

        @overload
        def __get__(self, obj: object, objtype: Any = ...) -> list[C]: ...

    def __get__(self, obj: Any, objtype: Any = None) -> Any:
        if obj is None:
            return self
        return obj._groups.get(self.name, [])


def repeating_group[C: Component](
    count: RegisterField[int] | int,
    component_class: type[C],
    *,
    stride: int,
) -> RepeatingGroupField[C]:
    """A repeated sub-block whose instance count is read from a register at poll time.

    Declares, on a parent ``Component``, a list of ``component_class`` instances
    sized at runtime — the runtime-counted counterpart to ``index`` / ``stride``,
    for a device that advertises its repeat count (a SunSpec multiple-MPPT model's
    ``N`` point, a meter's channel count) instead of fixing it in the layout::

        class MPPTModule(Component):              # one module, at instance 0
            dc_w = integer(11, scale_register=2)
            dc_v = integer(10, scale_register=1)

        class Inverter(Component):
            modules = repeating_group(uint16(8), MPPTModule, stride=20)

        inv = Inverter(unit)
        await inv.async_update()
        inv.modules              # list[MPPTModule]
        inv.modules[0].dc_w      # typed per-instance access; writes via the instance

    ``count`` is a :class:`RegisterField` read each poll, or a fixed ``int`` —
    a fixed count is static, so its instances fold into the parent's normal read
    instead of taking the two-phase path. ``component_class`` models one instance
    at instance 0's addresses; instance *i* is read at ``base_offset = i * stride``
    (so ``stride`` is the block length). An unimplemented or unreadable count
    yields no instances.
    """
    if stride <= 0:
        raise ValueError(f"repeating_group stride must be > 0, got {stride}")
    if isinstance(count, int) and count < 0:
        raise ValueError(f"a fixed count must be >= 0, got {count}")
    return RepeatingGroupField(count, component_class, stride=stride)
