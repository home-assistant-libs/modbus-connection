"""The ``Component`` base class: a device sub-system of typed register fields."""

from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING, Any, overload

from ._component_base import _ComponentBase
from ._planning import (
    _MAX_GAP,
    _MAX_SPAN,
    Range,
    ReadItem,
    ReadPlan,
    RegisterSpace,
    Space,
)
from ._writing import write_bit_field, write_register_field
from .fields import RegisterField, _BitField

if TYPE_CHECKING:
    from .._protocol import ModbusUnit


class Component(_ComponentBase):
    """A device sub-system whose attributes map to registers, coils and inputs.

    Subclasses declare ``RegisterField`` / ``CoilField`` / ``DiscreteInputField``
    descriptors (usually via the typed factories). Each component reads only its
    own registers, so it can refresh independently; listeners registered via
    :meth:`add_update_listener` fire after each update.

    Register fields live in one space, holding (FC03, default) or input (FC04) via
    :attr:`register_space`; input registers are read-only. Bit fields carry their
    own space — ``coil`` (FC01, writable) and ``discrete_input`` (FC02, read-only)
    — may be mixed, and are read separately. Declare :attr:`register_ranges` /
    :attr:`coil_ranges` / :attr:`discrete_ranges` (as class attributes or per
    instance) so pooled reads never cross an unreadable gap.

    ``base_offset`` places the whole declared layout at another base address:
    it is added to **every** address the component touches — fields, bits,
    :func:`repeating_group` counts and ``scale_register`` addresses — on reads
    and writes alike. Declare the layout once (relative to the block start,
    or at a default location) and instantiate it wherever the block actually
    sits, e.g. a SunSpec model at its discovered address. Repeated identical
    sub-units are addressed per instance instead: pass ``index`` (1-based) with
    a per-field ``stride`` (the two compose as
    ``field.address + field.stride * (index - 1)``, with ``scale_register``
    following ``scale_register_stride``), or model them as a
    :func:`repeating_group` — its instances shift per instance while their
    ``scale_register`` addresses keep following the parent's block, since a
    repeating block's scale factors usually sit in the shared fixed part of the
    model. A sub-unit that instead carries its own scale factors sets the
    :attr:`scale_in_block` class attribute, moving each instance's scale
    registers with its shift too.

    The read plan is derived from the static field layout and cached on the first
    :meth:`async_update`. The fields and ranges are read once then; to change the
    layout, build a new component.
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

    # Set on a repeating sub-unit whose scale factors live inside its own block:
    # its ``scale_register`` addresses then shift with the instance instead of
    # naming a shared scale factor in the parent's fixed block (the default). No
    # effect on a non-repeating component, whose instance offset is 0.
    scale_in_block: bool = False

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
        self,
        unit: ModbusUnit,
        index: int = 1,
        *,
        base_offset: int = 0,
        _instance_offset: int = 0,
    ) -> None:
        self._unit = unit
        self._index = index
        self._base_offset = base_offset
        # Internal, set by repeating_group when building instances: the
        # per-instance shift within the parent's block, applied to fields and
        # bits but not to scale registers (those follow the block itself),
        # unless the sub-unit sets ``scale_in_block``.
        self._instance_offset = _instance_offset
        self._values: dict[str, Any] = {}
        self._bits: dict[str, bool | None] = {}
        # Set up listener and repeating_group state; fixed-count groups'
        # instances are built now so they fold into the normal read plan like
        # ordinary fields.
        self._build_groups()

    @property
    def _count_space(self) -> RegisterSpace:
        # a count register is read from this component's own register space
        return self.register_space

    def _scale_address(self, field: RegisterField[Any]) -> int:
        """Resolve a field's scale-register address.

        Scale registers move with the block (``base_offset``) but not with a
        repeating instance's shift — a repeated sub-unit's scale factors sit
        in the parent's shared fixed block. A sub-unit that sets
        ``scale_in_block`` moves its scale registers with the instance shift
        too — a block that carries its own scale factors.
        """
        assert field.scale_register is not None
        address = (
            field.scale_register
            + field.scale_register_stride * (self._index - 1)
            + self._base_offset
        )
        if self.scale_in_block:
            address += self._instance_offset
        return address

    def _address(self, field: RegisterField[Any] | _BitField) -> int:
        return (
            field.address
            + field.stride * (self._index - 1)
            + self._base_offset
            + self._instance_offset
        )

    # -- update --------------------------------------------------------------

    @cached_property
    def _read_items(self) -> list[ReadItem]:
        """This component's read targets, scale registers resolved.

        Derived once from the static field layout and cached for the instance's
        life; do not mutate the field set afterwards.
        """
        items = []
        for field in self._register_fields.values():
            scale_address = (
                self._scale_address(field) if field.scale_register is not None else None
            )
            items.append(
                ReadItem(
                    self._address(field),
                    field,
                    self._values,
                    self.register_space,
                    scale_address,
                )
            )
        for bit_field in self._bit_fields.values():
            items.append(
                ReadItem(
                    self._address(bit_field), bit_field, self._bits, bit_field.space
                )
            )
        return items + self._count_items + self._static_items

    def _build_plan(self) -> ReadPlan:
        ranges: dict[Space, tuple[Range, ...] | None] = {
            self.register_space: self.register_ranges,
            "coil": self.coil_ranges,
            "discrete": self.discrete_ranges,
        }
        return ReadPlan.build(
            self._read_items, ranges, max_gap=self.max_gap, max_span=self.max_span
        )

    async def async_update(self) -> None:
        """Read this component's registers and coils, then notify listeners.

        Reads only this sub-system's own registers, so it can refresh on its own.
        A device that owns several components can instead pool them into one
        bulk read with a :class:`ComponentGroup`. The block plan is built on the
        first call and reused on later polls.

        A :func:`repeating_group` field needs a second pass: the first read
        fetches the count (it is part of the read plan), then
        :meth:`async_update_repeating_groups` reads the sized-out instances.

        If the device answers one of the block reads with a Modbus exception
        response (e.g. illegal data address), this raises
        :class:`~modbus_connection.exceptions.BlockReadError` and the update is not
        partially applied — an exception on any block fails the whole update. A
        device with genuinely optional blocks should read those on a separate
        component so their absence doesn't fail this update.
        """
        await self._refresh(collect_raw=False)

    # -- writes --------------------------------------------------------------

    async def write(self, field: str, value: Any) -> None:
        """Write a writable register or coil by attribute name.

        Applies the field's ``index`` / ``stride`` / ``base_offset`` to resolve the
        address, then defers to the shared write path (``writable`` validator,
        FC06 / FC16 / ``force_fc16``); see :func:`._writing.write_register_field`.
        A dynamically-scaled field reads its scale factor fresh in the same
        write and encodes the value with it; a not-implemented scale factor
        raises ``ValueError``. Writing a read-only field or space raises
        ``AttributeError``. Override in a subclass for device-specific write
        sequencing.
        """
        if field in self._register_fields:
            register = self._register_fields[field]
            scale_address = (
                self._scale_address(register)
                if register.scale_register is not None
                else None
            )
            await write_register_field(
                self._unit,
                register,
                self._address(register),
                self.register_space,
                value,
                label=field,
                scale_address=scale_address,
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

    A sub-unit that carries its own scale factors inside its block sets the
    :attr:`Component.scale_in_block` class attribute, so each instance's scale
    registers shift with it instead of naming a shared scale factor in the
    parent's fixed block.
    """
    if stride <= 0:
        raise ValueError(f"repeating_group stride must be > 0, got {stride}")
    if isinstance(count, int) and count < 0:
        raise ValueError(f"a fixed count must be >= 0, got {count}")
    return RepeatingGroupField(count, component_class, stride=stride)
