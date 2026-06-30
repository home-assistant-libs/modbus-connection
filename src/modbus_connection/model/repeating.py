"""The ``RepeatingGroup``: a repeated sub-block whose count is read at poll time.

``Component``'s ``index`` / ``stride`` model repeated sub-units whose *count is
known when you write the code* — you instantiate one ``Component`` per index.
Some devices instead advertise the count in a register, read at poll time: a
SunSpec multiple-MPPT model (160) carries an ``N`` point saying how many MPPT
modules follow, a multi-string meter reports its channel count, and so on. The
number of repeats is not known until the device is polled, and it can change.

``RepeatingGroup`` closes that gap. Give it the count source (a register field,
or a fixed ``int``) and the per-instance ``block`` template (fields with a base
address and a per-instance ``stride``); each update reads the count and returns
that many decoded instances. It is built on :class:`ManualComponent`, reusing
its pooled read engine.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from ._planning import _MAX_GAP, _MAX_SPAN
from .fields import CoilField, RegisterField
from .manual import ManualComponent

if TYPE_CHECKING:
    from .._protocol import ModbusUnit

UpdateListener = Callable[[], None]

_COUNT_KEY = "count"


def _at_instance(
    field: RegisterField[Any] | CoilField, index: int
) -> RegisterField[Any] | CoilField:
    """A copy of ``field`` at the absolute address for 0-based ``index``.

    Addresses as ``address + stride * index`` — so instance 0 sits at the
    template's own base address — and a ``sunssf`` scale register is strided the
    same way, so each instance scales off its own factor. The copy is read by a
    :class:`ManualComponent`, which addresses absolutely and ignores ``stride``.
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

    The runtime-counted counterpart to ``Component``'s static ``index`` / ``stride``:
    where a ``Component`` needs the number of sub-units known up front, a
    ``RepeatingGroup`` reads it from the device on every poll and reads that many
    instances.

    ``count`` is either a :class:`RegisterField` holding the live count (read each
    poll) or a fixed ``int``. ``block`` maps a name to the field for that point in
    one instance — its ``address`` is instance 0's address and its ``stride`` the
    step between instances. :meth:`async_update` returns one decoded ``dict`` per
    instance::

        group = RepeatingGroup(
            unit,
            count=ss.uint16(8),                       # model 160 "N" point
            block={
                "dc_w": ss.uint16(11, scale_register=2, stride=20),
                "dc_v": ss.uint16(10, scale_register=1, stride=20),
            },
        )
        instances = await group.async_update()
        # [{"dc_w": 95.0, "dc_v": 48.2}, {"dc_w": 90.0, "dc_v": 48.1}]
        len(instances)    # the count

    Reads are pooled into as few Modbus calls as possible (every instance, plus
    the count register, like any :class:`ManualComponent`). The plan is rebuilt
    only when the count changes, so a steady count costs a single set of reads;
    the first poll, and any poll where the count changes, costs one extra round
    trip — the count must be read before the instances it sizes can be planned. An
    unimplemented or unreadable count reads as ``0`` instances. The group is
    read-only; drive writes through a :class:`ManualComponent` or :class:`Component`.
    """

    def __init__(
        self,
        unit: ModbusUnit,
        *,
        count: RegisterField[int] | int,
        block: Mapping[str, RegisterField[Any] | CoilField],
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
        self._listeners: list[UpdateListener] = []
        self._n = 0
        self._mc = self._plan(0)

    def add_update_listener(self, listener: UpdateListener) -> Callable[[], None]:
        """Register a callback fired after each update; returns an unsubscribe."""
        self._listeners.append(listener)

        def remove() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return remove

    def _plan(self, n: int) -> ManualComponent:
        """A ManualComponent reading ``n`` instances (plus the count, if a field)."""
        mc = ManualComponent(self._unit, max_gap=self._max_gap, max_span=self._max_span)
        if isinstance(self._count, RegisterField):
            mc.add(_COUNT_KEY, self._count)
        for index in range(n):
            for name, field in self._block.items():
                mc.add(f"#{index}.{name}", _at_instance(field, index))
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

        for listener in list(self._listeners):
            listener()
        return [
            {name: self._mc.get(f"#{index}.{name}") for name in self._block}
            for index in range(self._n)
        ]
