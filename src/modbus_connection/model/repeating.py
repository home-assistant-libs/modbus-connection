"""The ``RepeatingGroup``: a repeated sub-block whose count is read at poll time.

``Component``'s ``index`` / ``stride`` model repeated sub-units whose *count is
known when you write the code*. Some devices instead advertise the count in a
register: a SunSpec multiple-MPPT model (160) has an ``N`` point saying how many
modules follow, a multi-string meter reports its channel count. The count isn't
known until the device is polled.

``RepeatingGroup`` is a thin wrapper over :class:`ManualComponent` for that case.
It reads the count, expands the per-instance ``block`` into that many
stride-offset copies, and returns one decoded ``dict`` per instance — caching the
read plan so a steady count re-reads in a single pooled call.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ._planning import _MAX_GAP, _MAX_SPAN
from .fields import CoilField, RegisterField
from .manual import ManualComponent

if TYPE_CHECKING:
    from .._protocol import ModbusUnit

Field = RegisterField[Any] | CoilField

_COUNT_KEY = "count"


def _at_instance(field: Field, index: int) -> Field:
    """A copy of ``field`` at the absolute address for 0-based ``index``.

    ``address + stride * index`` (instance 0 sits at the template's own address);
    a ``sunssf`` scale register is strided the same way so each instance scales
    off its own factor. ``ManualComponent`` addresses absolutely, so the copy's
    own ``stride`` is irrelevant.
    """
    clone = copy.copy(field)
    clone.address = field.address + field.stride * index
    if isinstance(field, RegisterField) and field.scale_register is not None:
        clone.scale_register = (
            field.scale_register + field.scale_register_stride * index
        )
    return clone


class RepeatingGroup:
    """A repeated sub-block whose instance count is read from a register at poll time.

    ``count`` is a fixed ``int`` or a :class:`RegisterField` read from the device.
    ``block`` maps a name to the field for that point in one instance — its
    ``address`` is instance 0's address and its ``stride`` the step between
    instances. :meth:`async_update` returns one decoded ``dict`` per instance::

        group = RepeatingGroup(
            unit,
            count=ss.uint16(8),                       # model 160 "N" point
            block={"dc_w": ss.uint16(11, scale_register=2, stride=20)},
        )
        instances = await group.async_update()        # [{"dc_w": 95.0}, {"dc_w": 90.0}]
        len(instances)                                # the count

    Every instance is pooled into as few Modbus reads as possible. The read plan
    is cached and rebuilt only when the count changes, so a steady count re-reads
    in one pooled call; the first poll, and any poll where the count changes,
    costs one extra round trip (the count must be read before the instances it
    sizes). An unimplemented or unreadable count yields no instances. The group is
    read-only; drive writes through a :class:`ManualComponent` or :class:`Component`.
    """

    def __init__(
        self,
        unit: ModbusUnit,
        *,
        count: RegisterField[int] | int,
        block: Mapping[str, Field],
        max_gap: int = _MAX_GAP,
        max_span: int = _MAX_SPAN,
    ) -> None:
        if not block:
            raise ValueError("a RepeatingGroup needs at least one block field")
        if isinstance(count, int) and count < 0:
            raise ValueError(f"a fixed count must be >= 0, got {count}")
        self._unit = unit
        self._count = count
        self._block = dict(block)
        self._max_gap = max_gap
        self._max_span = max_span
        self._n = 0
        self._mc = self._plan(0)

    def _plan(self, n: int) -> ManualComponent:
        """A ManualComponent reading ``n`` instances (plus the count, if a field)."""
        mc = ManualComponent(self._unit, max_gap=self._max_gap, max_span=self._max_span)
        if isinstance(self._count, RegisterField):
            mc.add(_COUNT_KEY, self._count)
        for index in range(n):
            for name, field in self._block.items():
                mc.add(f"{index}:{name}", _at_instance(field, index))
        return mc

    async def async_update(self) -> list[dict[str, Any]]:
        """Read the count, (re)size the instances to match, and read them all.

        Returns one decoded ``dict`` per instance (``len`` is the count).
        """
        await self._mc.async_update()
        if isinstance(self._count, int):
            n = self._count
        else:
            value = self._mc.get(_COUNT_KEY)
            n = max(0, int(value)) if value is not None else 0
        if n != self._n:
            self._n = n
            self._mc = self._plan(n)
            await self._mc.async_update()
        return [
            {name: self._mc.get(f"{index}:{name}") for name in self._block}
            for index in range(self._n)
        ]
