"""The ``Component`` base class: a device sub-system of typed register fields."""

from __future__ import annotations

from collections.abc import Callable
from functools import cached_property
from typing import TYPE_CHECKING, Any

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

    The read plan (which blocks to fetch) is derived from the static field layout
    and cached on first :meth:`async_update`, so each subsequent poll reuses it
    rather than re-planning. The fields and ``register_ranges`` / ``coil_ranges``
    are read once at that point; mutating them afterwards is not supported — set
    the ranges before the first update, and build a new component to change the
    field layout.
    """

    _register_fields: dict[str, RegisterField[Any]] = {}
    _bit_fields: dict[str, _BitField] = {}

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
        for klass in reversed(cls.__mro__):
            for name, value in vars(klass).items():
                if isinstance(value, RegisterField):
                    registers[name] = value
                elif isinstance(value, _BitField):
                    bits[name] = value
        cls._register_fields = registers
        cls._bit_fields = bits

    def __init__(self, unit: ModbusUnit, index: int = 1) -> None:
        self._unit = unit
        self._index = index
        self._values: dict[str, Any] = {}
        self._bits: dict[str, bool | None] = {}
        self._listeners: list[UpdateListener] = []

    def _address(self, field: RegisterField[Any] | _BitField) -> int:
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
                RegisterItem(
                    self._address(field),
                    field,
                    self._values,
                    scale_address,
                    self.register_space,
                )
            )
        return items

    @cached_property
    def bit_items(self) -> list[BitItem]:
        """This component's bit read targets (coils and discrete inputs)."""
        return [(self._address(f), f, self._bits) for f in self._bit_fields.values()]

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
        """Fire every registered update listener."""
        for listener in list(self._listeners):
            listener()

    async def async_update(self) -> None:
        """Read this component's registers and coils, then notify listeners.

        Reads only this sub-system's own registers, so it can refresh on its own.
        A device that owns several components can instead pool their
        :attr:`register_items` / :attr:`bit_items` into one bulk read. The block
        plan is built on the first call and reused on later polls.
        """
        await _bulk_read_registers(
            self._unit, self.register_items, self._register_blocks
        )
        await _bulk_read_bits(self._unit, self.bit_items, self._bit_blocks)
        self.notify()

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
