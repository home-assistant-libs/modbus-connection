"""The ``Component`` base class: a device sub-system of typed register fields."""

from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING, Any, overload

from ._component_base import _ComponentBase
from ._const import _MAX_GAP, _MAX_SPAN, Range, RegisterSpace, Space
from ._planning import ReadItem, ReadPlan, _shift_ranges
from ._writing import write_bit_field, write_register_field
from .fields import RegisterField, _BitField

if TYPE_CHECKING:
    from .._protocol import ModbusUnit


class Component(_ComponentBase):
    """Map a device subsystem to typed register and bit attributes."""

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
    # distinct spaces with their own readable maps. They are part of the declared
    # layout, so they are stated in the same coordinates as the field addresses and
    # move with the component (see ``_resolved_ranges``).
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
        """Resolve a field's scale-register address."""
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
        """Return this component's read targets."""
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
        items.extend(
            ReadItem(self._address(field), field, self._bits, field.space)
            for field in self._bit_fields.values()
        )
        return items + self._count_items + self._static_items

    def _resolved_ranges(self) -> dict[Space, tuple[Range, ...] | None]:
        """This component's readable ranges at the addresses it actually reads.

        The declared ranges share the coordinate system of the declared field
        addresses, so they take the same shift ``_address`` applies — everything
        that moves the whole block. A per-field ``stride`` is not part of that
        shift, so a layout addressed by ``index`` states its ranges absolutely.

        A fixed-count ``repeating_group``'s instances are read from this
        component's plan, so their maps are merged in (see
        ``_with_static_ranges``).

        Raises ``ValueError`` if this component's map conflicts with an instance's.
        """
        offset = self._base_offset + self._instance_offset
        return self._with_static_ranges(
            {
                self.register_space: _shift_ranges(self.register_ranges, offset),
                "coil": _shift_ranges(self.coil_ranges, offset),
                "discrete": _shift_ranges(self.discrete_ranges, offset),
            }
        )

    def _build_plan(self) -> ReadPlan:
        return ReadPlan.build(
            self._read_items,
            self._resolved_ranges(),
            max_gap=self.max_gap,
            max_span=self.max_span,
        )

    async def async_update(self) -> None:
        """Read this component and notify its listeners.

        Raises ``BlockReadError`` if the device rejects a block.
        """
        await self._refresh(collect_raw=False)

    # -- writes --------------------------------------------------------------

    async def write(self, field: str, value: Any) -> None:
        """Write a writable register or coil by attribute name.

        Raises ``AttributeError`` for an unknown or read-only field and
        ``ValueError`` if the value cannot be scaled.
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
    """Describe repeated subcomponents."""

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
    """Create a repeated subcomponent field.

    Raises ``ValueError`` for a non-positive stride or negative fixed count.
    """
    if stride <= 0:
        raise ValueError(f"repeating_group stride must be > 0, got {stride}")
    if isinstance(count, int) and count < 0:
        raise ValueError(f"a fixed count must be >= 0, got {count}")
    return RepeatingGroupField(count, component_class, stride=stride)
