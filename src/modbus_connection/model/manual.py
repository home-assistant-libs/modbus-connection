"""The ``ManualComponent``: a register/coil read+write group built at runtime."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .._types import BitSpace
from ._planning import (
    _MAX_GAP,
    _MAX_SPAN,
    BitItem,
    Range,
    RegisterItem,
    RegisterSpace,
    _bulk_read_bits,
    _bulk_read_registers,
    _plan_bit_blocks,
    _plan_register_blocks,
)
from ._repeating import _RepeatingGroups
from ._writing import write_bit_field, write_register_field
from .component import RepeatingGroupField, UpdateListener
from .fields import RegisterField, _BitField

if TYPE_CHECKING:
    from .._protocol import ModbusUnit

# Cached read plan: register items + their per-space blocks, bit items + theirs.
_Plan = tuple[
    list[RegisterItem],
    dict[RegisterSpace, list[tuple[int, int]]],
    list[BitItem],
    dict[BitSpace, list[tuple[int, int]]],
]


class ManualComponent(_RepeatingGroups):
    """A register/bit read group assembled imperatively at runtime.

    The imperative sibling of :class:`Component`: instead of declaring fields as
    class attributes, you :meth:`add` them by key at runtime — for a consumer
    that maps its layout from config (e.g. YAML) rather than a typed class. It
    pools its targets into as few Modbus reads as possible just like a
    ``Component``, and can mix all four tables in one update — holding (FC03) and
    input (FC04) registers, coils (FC01) and discrete inputs (FC02).

    Values come out via :meth:`get` (and the dict returned by
    :meth:`async_update`); there is no typed attribute access, since there is no
    class to hang descriptors on. Adding or removing a target invalidates the
    cached read plan, which is rebuilt on the next update.

    Pass a per-table ``*_ranges`` to constrain reads to a device's readable
    address ranges, like ``Component.register_ranges`` / ``coil_ranges``::

        ManualComponent(unit, holding_ranges=((0, 40),), input_ranges=((500, 520),))

    A table left ``None`` falls back to gap-based planning.
    """

    def __init__(
        self,
        unit: ModbusUnit,
        *,
        max_gap: int = _MAX_GAP,
        max_span: int = _MAX_SPAN,
        holding_ranges: tuple[Range, ...] | None = None,
        input_ranges: tuple[Range, ...] | None = None,
        coil_ranges: tuple[Range, ...] | None = None,
        discrete_ranges: tuple[Range, ...] | None = None,
    ) -> None:
        self._unit = unit
        self._max_gap = max_gap
        self._max_span = max_span
        # Readable address ranges per table; a table left None falls back to
        # gap-based planning, like Component does.
        self._holding_ranges = holding_ranges
        self._input_ranges = input_ranges
        self._coil_ranges = coil_ranges
        self._discrete_ranges = discrete_ranges
        self._registers: dict[str, tuple[RegisterField[Any], RegisterSpace]] = {}
        self._bits: dict[str, _BitField] = {}
        self._values: dict[str, Any] = {}
        self._listeners: list[UpdateListener] = []
        self._plan: _Plan | None = None
        # repeating_group support (counts read from holding); groups are added by
        # key like any other target. base_offset stays 0 — addresses are absolute.
        self._static_groups: dict[str, RepeatingGroupField[Any]] = {}
        self._repeating_fields: dict[str, RepeatingGroupField[Any]] = {}
        self._build_groups()

    # -- membership ----------------------------------------------------------

    def add(
        self,
        key: str,
        target: RegisterField[Any] | _BitField | RepeatingGroupField[Any],
        *,
        space: RegisterSpace | None = None,
    ) -> None:
        """Add a read target under ``key``, replacing any existing one.

        ``target`` is a register field (from ``gauge`` / ``integer`` / ``uint32``
        / ``sunspec.*`` / ...) read from ``space`` ``"holding"`` (default) or
        ``"input"``, or a bit field from ``coil()`` (FC01) / ``discrete_input()``
        (FC02) — whose own space is fixed, so ``space`` does not apply. The field's
        ``address`` is absolute. ``target`` may also be a :func:`repeating_group`
        (``space`` does not apply); its instances come out via :meth:`get`.
        """
        self.remove(key)  # replace any existing target, and invalidate the plan
        if isinstance(target, RepeatingGroupField):
            if space is not None:
                raise ValueError("space does not apply to a repeating_group")
            if isinstance(target.count, int):
                self._static_groups[key] = target
                self._groups[key] = self._build_instances(target, 0, target.count)
            else:
                self._repeating_fields[key] = target
        elif isinstance(target, _BitField):
            if space is not None:
                raise ValueError(
                    "space is fixed by the field type for bits; "
                    "use coil() or discrete_input()"
                )
            target.name = key  # the bit reader scatters into store[field.name]
            self._bits[key] = target
        elif isinstance(target, RegisterField):
            register_space = space or "holding"
            if register_space not in ("holding", "input"):
                raise ValueError(
                    f"register space must be 'holding' or 'input', got {space!r}"
                )
            target.name = key  # the register reader scatters into store[field.name]
            self._registers[key] = (target, register_space)
        else:
            raise TypeError(
                f"target must be a RegisterField, a bit field or a repeating_group, "
                f"got {type(target).__name__}"
            )

    def remove(self, key: str) -> None:
        """Remove the target under ``key``; invalidates the cached plan."""
        self._registers.pop(key, None)
        self._bits.pop(key, None)
        self._values.pop(key, None)
        self._static_groups.pop(key, None)
        self._repeating_fields.pop(key, None)
        if self._groups.pop(key, None) is not None:
            self._instance_group = None
        self._invalidate_group_cache()
        self._plan = None

    # -- values --------------------------------------------------------------

    def get(self, key: str) -> Any:
        """The value decoded for ``key`` on the last update (None if not yet read).

        For a :func:`repeating_group` key, returns its ``list`` of sub-component
        instances (empty before the first update sizes a register-count group).
        """
        if key in self._static_groups or key in self._repeating_fields:
            return self._groups.get(key, [])
        return self._values.get(key)

    @property
    def values(self) -> dict[str, Any]:
        """A copy of all decoded values from the last update."""
        return dict(self._values)

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

    def _build_plan(self) -> _Plan:
        register_items = [
            RegisterItem(
                field.address, field, self._values, field.scale_register, space
            )
            for field, space in self._registers.values()
        ]
        # Fold in each group's count register and any fixed-count instances, so the
        # normal read fetches the counts and static instances in one pass.
        register_items += self._count_items + self._static_register_items
        register_blocks = _plan_register_blocks(
            register_items,
            {"holding": self._holding_ranges, "input": self._input_ranges},
            max_gap=self._max_gap,
            max_span=self._max_span,
        )
        bit_items: list[BitItem] = [
            (field.address, field, self._values) for field in self._bits.values()
        ]
        bit_items += self._static_bit_items
        bit_blocks = _plan_bit_blocks(
            bit_items,
            {"coil": self._coil_ranges, "discrete": self._discrete_ranges},
            max_gap=self._max_gap,
            max_span=self._max_span,
        )
        return register_items, register_blocks, bit_items, bit_blocks

    async def async_update(self) -> dict[str, Any]:
        """Read every target in pooled block reads; return the decoded values.

        The read plan is built on the first call and reused until a target is
        added or removed.
        """
        if self._plan is None:
            self._plan = self._build_plan()
        register_items, register_blocks, bit_items, bit_blocks = self._plan
        await _bulk_read_registers(self._unit, register_items, register_blocks)
        await _bulk_read_bits(self._unit, bit_items, bit_blocks)
        await self.async_update_repeating_groups()
        for listener in list(self._listeners):
            listener()
        return dict(self._values)

    # -- writes --------------------------------------------------------------

    async def write(self, key: str, value: Any) -> None:
        """Write a writable register or coil by key (holding / coil only).

        Shares :meth:`Component.write`'s behaviour (``writable`` validator, FC06 /
        FC16 with ``force_fc16``) via the same write helpers. A discrete input is
        read-only, so writing one raises.
        """
        if key in self._registers:
            field, register_space = self._registers[key]
            await write_register_field(
                self._unit, field, field.address, register_space, value, label=key
            )
        elif key in self._bits:
            await write_bit_field(
                self._unit, self._bits[key], self._bits[key].address, value, label=key
            )
        else:
            raise AttributeError(f"unknown key {key!r}")
