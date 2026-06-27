"""The ``RepeatingGroup``: a repeated sub-block whose count is read at poll time.

``Component``'s ``index`` / ``stride`` model repeated sub-units whose *count is
known when you write the code* — you instantiate one ``Component`` per index.
Some devices instead advertise the count in a register, read at poll time: a
SunSpec multiple-MPPT model (160) carries an ``N`` point saying how many MPPT
modules follow, a multi-string meter reports its channel count, and so on. The
number of repeats is not known until the device is polled, and it can change.

``RepeatingGroup`` closes that gap. Give it the count source (a register field,
or a fixed ``int``), the per-instance ``block`` template (fields with a base
address and a per-instance ``stride``), and optional fixed ``header`` fields. On
each update it reads the count, materialises that many strided instances, and
reads them — re-planning only when the count changes. It is built on
:class:`ManualComponent`, reusing its pooled, re-plannable read engine.
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
_HEADER_PREFIX = "header."


def _at_instance(
    field: RegisterField[Any] | CoilField, index: int
) -> RegisterField[Any] | CoilField:
    """A copy of ``field`` resolved to its absolute address for 1-based ``index``.

    Mirrors ``Component`` addressing — ``address + stride * (index - 1)`` — but
    bakes the result into a standalone field (``stride`` zeroed) so it can be read
    by a :class:`ManualComponent`, which addresses absolutely. A ``sunssf`` scale
    register is strided the same way so each instance scales off its own factor.
    """
    clone = copy.copy(field)
    clone.address = field.address + field.stride * (index - 1)
    clone.stride = 0
    if isinstance(field, RegisterField) and field.scale_register is not None:
        clone.scale_register = field.scale_register + field.scale_register_stride * (
            index - 1
        )
        clone.scale_register_stride = 0
    return clone


class RepeatingGroup:
    """A repeated sub-block whose instance count is read from a register at poll time.

    The runtime-counted counterpart to ``Component``'s static ``index`` / ``stride``:
    where a ``Component`` needs the number of sub-units known up front, a
    ``RepeatingGroup`` reads it from the device on every poll and grows or shrinks
    the instances it reads to match.

    ``count`` is either a :class:`RegisterField` holding the live count (read each
    poll) or a fixed ``int``. ``block`` maps a name to the field for that point in
    one instance — its ``address`` is the first instance's address and its
    ``stride`` the step between instances. ``header`` (optional) maps fixed,
    non-repeating fields read once per poll (e.g. a shared event flag, or the
    count point itself if you also want it surfaced).

    :meth:`async_update` returns the structured reading and caches it::

        group = RepeatingGroup(
            unit,
            count=ss.uint16(8),                       # model 160 "N" point
            block={
                "dc_w": ss.uint16(11, scale_register=2, stride=20),
                "dc_v": ss.uint16(10, scale_register=1, stride=20),
            },
        )
        data = await group.async_update()
        # {"count": 2, "instances": [{"dc_w": 95.0, "dc_v": 48.2}, {...}]}

    It reads in as few Modbus calls as possible (the count, header and every
    instance are pooled like any :class:`ManualComponent`). The first poll, and
    any poll where the count changes, costs one extra round trip: the count must
    be read before the instances it sizes can be planned. The group is read-only;
    drive writes through a :class:`ManualComponent` or :class:`Component`.
    """

    def __init__(
        self,
        unit: ModbusUnit,
        *,
        count: RegisterField[int] | int,
        block: Mapping[str, RegisterField[Any] | CoilField],
        header: Mapping[str, RegisterField[Any] | CoilField] | None = None,
        max_gap: int = _MAX_GAP,
        max_span: int = _MAX_SPAN,
    ) -> None:
        if not block:
            raise ValueError("a RepeatingGroup needs at least one block field")
        if isinstance(count, int) and count < 0:
            raise ValueError(f"a fixed count must be >= 0, got {count}")
        self._count = count
        self._block = dict(block)
        self._header = dict(header or {})
        self._mc = ManualComponent(unit, max_gap=max_gap, max_span=max_span)
        self._listeners: list[UpdateListener] = []
        self._instance_keys: list[str] = []
        # The instance count currently planned into the ManualComponent; None until
        # the first update configures it, so the count is always applied once.
        self._configured: int | None = None

        if isinstance(count, RegisterField):
            self._mc.add(_COUNT_KEY, count)
        for name, field in self._header.items():
            self._mc.add(f"{_HEADER_PREFIX}{name}", field)

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

    # -- values --------------------------------------------------------------

    @property
    def count(self) -> int:
        """Instances read on the last update (always matches ``len(instances)``).

        An unimplemented or unreadable count register normalises to ``0``.
        """
        if isinstance(self._count, int):
            return self._count
        return _normalize(self._mc.get(_COUNT_KEY))

    def instance(self, index: int) -> dict[str, Any]:
        """The decoded block values for 1-based ``index`` (KeyError if out of range)."""
        configured = self._configured or 0
        if not 1 <= index <= configured:
            raise KeyError(index)
        return {name: self._mc.get(f"#{index}.{name}") for name in self._block}

    def header(self) -> dict[str, Any]:
        """The decoded fixed-header values from the last update."""
        return {name: self._mc.get(f"{_HEADER_PREFIX}{name}") for name in self._header}

    # -- update --------------------------------------------------------------

    def _reconfigure(self, count: int) -> None:
        """Plan exactly ``count`` instances into the ManualComponent."""
        for key in self._instance_keys:
            self._mc.remove(key)
        self._instance_keys = []
        for index in range(1, count + 1):
            for name, field in self._block.items():
                key = f"#{index}.{name}"
                self._mc.add(key, _at_instance(field, index))
                self._instance_keys.append(key)
        self._configured = count

    async def async_update(self) -> dict[str, Any]:
        """Read the count, (re)size the instances to match, and read them all.

        Returns ``{"count": n, "instances": [...], "header": {...}}`` (``header``
        only when header fields were given). The instance set is re-planned only
        when the count changes, so a steady count costs a single set of reads.
        """
        if isinstance(self._count, int):
            if self._configured != self._count:
                self._reconfigure(self._count)
            await self._mc.async_update()
        else:
            await self._mc.async_update()
            live = _normalize(self._mc.get(_COUNT_KEY))
            if live != self._configured:
                self._reconfigure(live)
                await self._mc.async_update()

        for listener in list(self._listeners):
            listener()
        return self._structured()

    def _structured(self) -> dict[str, Any]:
        configured = self._configured or 0
        result: dict[str, Any] = {
            "count": self.count,
            "instances": [self.instance(i) for i in range(1, configured + 1)],
        }
        if self._header:
            result["header"] = self.header()
        return result


def _normalize(value: Any) -> int:
    """A read count as a non-negative ``int`` (``None`` / negative -> ``0``)."""
    if value is None:
        return 0
    return max(0, int(value))
