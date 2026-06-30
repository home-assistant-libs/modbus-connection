"""Read a repeated sub-block whose instance count is read at poll time.

``Component``'s ``index`` / ``stride`` model repeated sub-units whose *count is
known when you write the code*. Some devices instead advertise the count in a
register: a SunSpec multiple-MPPT model (160) has an ``N`` point saying how many
modules follow, a multi-string meter reports its channel count. The count isn't
known until the device is polled.

:func:`read_repeating` is a thin helper over :class:`ManualComponent` for that
case: it reads the count, expands the per-instance ``block`` into that many
stride-offset copies, and returns one decoded ``dict`` per instance.
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


async def read_repeating(
    unit: ModbusUnit,
    *,
    count: RegisterField[int] | int,
    block: Mapping[str, Field],
    max_gap: int = _MAX_GAP,
    max_span: int = _MAX_SPAN,
) -> list[dict[str, Any]]:
    """Read ``count`` repeats of ``block`` and return one decoded dict per instance.

    ``count`` is a fixed ``int`` or a :class:`RegisterField` read from the device.
    ``block`` maps a name to the field for that point in one instance — its
    ``address`` is instance 0's address and its ``stride`` the step between
    instances. The instances are pooled into as few Modbus reads as possible; a
    register ``count`` costs one extra read (it must be read before the instances
    it sizes). An unimplemented or unreadable count yields no instances.

        instances = await read_repeating(
            unit,
            count=ss.uint16(8),                       # model 160 "N" point
            block={"dc_w": ss.uint16(11, scale_register=2, stride=20)},
        )
        # [{"dc_w": 95.0}, {"dc_w": 90.0}]
    """
    mc = ManualComponent(unit, max_gap=max_gap, max_span=max_span)
    if isinstance(count, int):
        n = count
    else:
        mc.add("count", count)
        await mc.async_update()
        value = mc.get("count")
        n = max(0, int(value)) if value is not None else 0
    for index in range(n):
        for name, field in block.items():
            mc.add(f"{index}:{name}", _at_instance(field, index))
    values = await mc.async_update()
    return [{name: values[f"{index}:{name}"] for name in block} for index in range(n)]
