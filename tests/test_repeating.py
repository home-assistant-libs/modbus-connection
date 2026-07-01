"""Tests for repeating_group — a runtime-counted list of sub-components."""

from __future__ import annotations

import pytest

from modbus_connection.mock import MockModbusConnection, MockModbusUnit
from modbus_connection.model import (
    Component,
    ComponentGroup,
    integer,
    repeating_group,
)
from modbus_connection.model.sunspec import uint16


def _unit() -> MockModbusUnit:
    return MockModbusConnection().for_unit(1)


class Module(Component):
    """One repeating sub-unit, modelled at instance 0's addresses."""

    w = integer(11, signed=False)
    v = integer(10, signed=False)


class _Spy:
    """Wraps a unit and records (function, address, count) for every read."""

    def __init__(self, inner: MockModbusUnit) -> None:
        self._inner = inner
        self.reads: list[tuple[str, int, int]] = []

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        self.reads.append(("holding", address, count))
        return await self._inner.read_holding_registers(address, count)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


async def test_count_register_sizes_typed_instances() -> None:
    class Inverter(Component):
        modules = repeating_group(uint16(8), Module, stride=20)

    unit = _unit()
    # count=2 at 8; module 0 at 10/11, module 1 shifted +20 -> 30/31
    unit.holding.update({8: 2, 10: 480, 11: 100, 30: 482, 31: 95})
    inv = Inverter(unit)
    await inv.async_update()

    assert isinstance(inv.modules, list) and len(inv.modules) == 2
    assert isinstance(inv.modules[0], Module)
    assert [(m.v, m.w) for m in inv.modules] == [(480, 100), (482, 95)]


async def test_empty_before_first_update() -> None:
    class Inverter(Component):
        modules = repeating_group(uint16(8), Module, stride=20)

    assert Inverter(_unit()).modules == []


async def test_count_change_resizes() -> None:
    class Inverter(Component):
        modules = repeating_group(uint16(8), Module, stride=20)

    unit = _unit()
    unit.holding.update({8: 1, 11: 1, 31: 2, 51: 3})
    inv = Inverter(unit)
    await inv.async_update()
    assert [m.w for m in inv.modules] == [1]

    unit.holding[8] = 3  # device now reports three modules
    await inv.async_update()
    assert [m.w for m in inv.modules] == [1, 2, 3]

    unit.holding[8] = 1  # ...and back down
    await inv.async_update()
    assert [m.w for m in inv.modules] == [1]


async def test_unimplemented_count_yields_no_instances() -> None:
    class Inverter(Component):
        modules = repeating_group(uint16(8), Module, stride=20)

    unit = _unit()
    unit.holding[8] = 0xFFFF  # uint16 unimplemented -> None -> 0 instances
    inv = Inverter(unit)
    await inv.async_update()
    assert inv.modules == []


async def test_fixed_int_count() -> None:
    class Inverter(Component):
        modules = repeating_group(2, Module, stride=20)

    unit = _unit()
    unit.holding.update({11: 100, 31: 95})
    inv = Inverter(unit)
    await inv.async_update()
    assert [m.w for m in inv.modules] == [100, 95]


async def test_fixed_count_reads_in_one_pass() -> None:
    class Inverter(Component):
        modules = repeating_group(2, Module, stride=2)

    inner = _unit()
    # module 0 at v=10/w=11, module 1 shifted +2 -> v=12/w=13; all adjacent
    inner.holding.update({10: 1, 11: 2, 12: 3, 13: 4})
    unit = _Spy(inner)
    inv = Inverter(unit)  # type: ignore[arg-type]
    await inv.async_update()
    assert [(m.v, m.w) for m in inv.modules] == [(1, 2), (3, 4)]
    # A fixed count is static: its instances fold into the normal read — one
    # pooled block, no second pass.
    assert unit.reads == [("holding", 10, 4)]


async def test_parent_own_fields_read_alongside() -> None:
    class Inverter(Component):
        serial = integer(0, signed=False)
        modules = repeating_group(uint16(8), Module, stride=20)

    unit = _unit()
    unit.holding.update({0: 1234, 8: 1, 11: 7})
    inv = Inverter(unit)
    await inv.async_update()
    assert inv.serial == 1234
    assert [m.w for m in inv.modules] == [7]


async def test_instances_pooled_into_one_read() -> None:
    class Inverter(Component):
        modules = repeating_group(uint16(0), Module, stride=2)

    inner = _unit()
    # count at 0; modules at 10/11 and 12/13 — adjacent, should pool into one read
    inner.holding.update({0: 2, 10: 1, 11: 2, 12: 3, 13: 4})
    unit = _Spy(inner)
    inv = Inverter(unit)  # type: ignore[arg-type]
    await inv.async_update()
    assert [(m.v, m.w) for m in inv.modules] == [(1, 2), (3, 4)]
    # Phase 1 reads the count; phase 2 reads all four module registers in one block.
    assert ("holding", 0, 1) in unit.reads
    assert ("holding", 10, 4) in unit.reads
    assert len(unit.reads) == 2


async def test_per_instance_shared_scale_register() -> None:
    class ScaledModule(Component):
        w = integer(11, scale_register=2)  # sunssf shared in the fixed block

    class Inverter(Component):
        modules = repeating_group(uint16(8), ScaledModule, stride=20)

    unit = _unit()
    unit.holding.update({8: 2, 2: (-2) & 0xFFFF, 11: 1234, 31: 5678})
    inv = Inverter(unit)
    await inv.async_update()
    # Both modules scale off the shared SF at addr 2 (not shifted by base_offset).
    assert inv.modules[0].w == pytest.approx(12.34)  # 1234 * 10**-2
    assert inv.modules[1].w == pytest.approx(56.78)  # 5678 * 10**-2


async def test_write_through_instance() -> None:
    class WModule(Component):
        setpoint = integer(11, signed=False, writable=True)

    class Inverter(Component):
        modules = repeating_group(2, WModule, stride=20)

    unit = _unit()
    inv = Inverter(unit)
    await inv.async_update()
    await inv.modules[1].write("setpoint", 42)  # module 1 -> address 11 + 20
    assert (await unit.read_holding_registers(31, 1)) == [42]


async def test_instance_listener_fires_once_per_update() -> None:
    # A register-count instance is read via ComponentGroup and notified via the
    # parent's notify(); it must fire exactly once, not twice.
    class Inverter(Component):
        modules = repeating_group(uint16(8), Module, stride=20)

    unit = _unit()
    unit.holding.update({8: 2, 11: 1, 31: 2})
    inv = Inverter(unit)
    await inv.async_update()  # sizes the two instances
    calls: list[int] = []
    inv.modules[0].add_update_listener(lambda: calls.append(1))
    await inv.async_update()
    assert calls == [1]


async def test_static_instance_listener_fires_via_parent() -> None:
    class Inverter(Component):
        modules = repeating_group(1, Module, stride=20)

    unit = _unit()
    unit.holding[11] = 5
    inv = Inverter(unit)
    calls: list[int] = []
    inv.modules[0].add_update_listener(lambda: calls.append(1))
    await inv.async_update()
    assert calls == [1]


async def test_static_group_pooled_in_component_group() -> None:
    # A fixed-count group's instances fold into register_items, so ComponentGroup
    # reads them in its pooled reads and its notify() cascades to them.
    class Inverter(Component):
        modules = repeating_group(2, Module, stride=20)

    class Meter(Component):
        power = integer(50, signed=False)

    unit = _unit()
    unit.holding.update({11: 100, 31: 95, 50: 7})  # module 0/1 at w=11/31; meter at 50
    inv, meter = Inverter(unit), Meter(unit)
    calls: list[int] = []
    inv.modules[0].add_update_listener(lambda: calls.append(1))

    await ComponentGroup(unit, [inv, meter]).async_update()

    assert [m.w for m in inv.modules] == [100, 95]
    assert meter.power == 7
    assert calls == [1]  # instance notified once, via the group's notify() cascade


async def test_dynamic_group_refreshed_by_component_group() -> None:
    # ComponentGroup reads the count in its pooled read, then drives each member's
    # async_update_repeating_groups() — so a register-count group updates in a group.
    class Inverter(Component):
        modules = repeating_group(uint16(8), Module, stride=20)

    class Meter(Component):
        power = integer(50, signed=False)

    unit = _unit()
    unit.holding.update({8: 2, 11: 100, 31: 95, 50: 7})
    inv, meter = Inverter(unit), Meter(unit)
    await ComponentGroup(unit, [inv, meter]).async_update()
    assert [m.w for m in inv.modules] == [100, 95]
    assert meter.power == 7


def test_factory_validates() -> None:
    with pytest.raises(ValueError, match="stride must be > 0"):
        repeating_group(uint16(8), Module, stride=0)
    with pytest.raises(ValueError, match="must be >= 0"):
        repeating_group(-1, Module, stride=20)
