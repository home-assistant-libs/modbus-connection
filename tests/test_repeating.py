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


async def test_read_raw_includes_repeating_instance_registers() -> None:
    class Inverter(Component):
        modules = repeating_group(uint16(8), Module, stride=20)

    unit = _unit()
    # count=2 at 8; module 0 at 10/11, module 1 shifted +20 -> 30/31
    unit.holding.update({8: 2, 10: 480, 11: 100, 30: 482, 31: 95})

    # No prior update: the raw read sizes the repeats and includes their data.
    raw = await Inverter(unit).async_read_raw()

    # The count register (8) plus both sized instances' registers are present,
    # raw and keyed by absolute address.
    assert raw["holding"] == {8: 2, 10: 480, 11: 100, 30: 482, 31: 95}


async def test_read_raw_notifies_each_repeating_instance_once() -> None:
    class Inverter(Component):
        modules = repeating_group(uint16(8), Module, stride=20)

    unit = _unit()
    unit.holding.update({8: 2, 10: 480, 11: 100, 30: 482, 31: 95})
    inv = Inverter(unit)
    await inv.async_update()  # size the instances so we can attach listeners

    counts = [0, 0]
    for i, module in enumerate(inv.modules):
        module.add_update_listener(lambda i=i: counts.__setitem__(i, counts[i] + 1))

    # The nested repeating read uses the non-notifying core; the top-level
    # notify() cascades to each instance exactly once (not twice).
    await inv.async_read_raw()
    assert counts == [1, 1]


async def test_read_raw_repeats_size_from_a_fresh_count() -> None:
    class Inverter(Component):
        modules = repeating_group(uint16(8), Module, stride=20)

    unit = _unit()
    unit.holding.update({8: 1, 10: 480, 11: 100})
    inv = Inverter(unit)

    raw = await inv.async_read_raw()
    assert raw["holding"] == {8: 1, 10: 480, 11: 100}  # one instance

    # The device now advertises two; a later raw read re-sizes and picks up the
    # second instance's registers.
    unit.holding.update({8: 2, 30: 482, 31: 95})
    raw = await inv.async_read_raw()
    assert raw["holding"] == {8: 2, 10: 480, 11: 100, 30: 482, 31: 95}


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
    # Both modules scale off the shared SF at addr 2 (not shifted per instance).
    assert inv.modules[0].w == pytest.approx(12.34)  # 1234 * 10**-2
    assert inv.modules[1].w == pytest.approx(56.78)  # 5678 * 10**-2


async def test_scale_in_block_shifts_scale_register_per_instance() -> None:
    # A repeating block that carries its own scale factor: the sunssf lives
    # inside each instance, so it must shift with the instance, not stay pinned
    # to the parent's fixed block.
    class Channel(Component):
        scale_in_block = True
        a = integer(0, scale_register=1)
        a_sf = integer(1)

    class Meter(Component):
        channels = repeating_group(uint16(4), Channel, stride=2)

    unit = _unit()
    # count=2 at 4; ch0 value/sf at 0/1, ch1 shifted +2 -> 2/3
    unit.holding.update({4: 2, 0: 1234, 1: (-2) & 0xFFFF, 2: 5678, 3: (-1) & 0xFFFF})
    inv = Meter(unit)
    await inv.async_update()
    assert inv.channels[0].a == pytest.approx(12.34)  # 1234 * 10**-2
    assert inv.channels[1].a == pytest.approx(567.8)  # 5678 * 10**-1


async def test_scale_in_block_with_base_offset() -> None:
    # scale_in_block composes with a discovered block's base_offset.
    class Channel(Component):
        scale_in_block = True
        a = integer(0, scale_register=1)

    class Meter(Component):
        channels = repeating_group(uint16(4), Channel, stride=2)

    unit = _unit()
    unit.holding.update(
        {104: 2, 100: 1234, 101: (-2) & 0xFFFF, 102: 5678, 103: (-1) & 0xFFFF}
    )
    inv = Meter(unit, base_offset=100)
    await inv.async_update()
    assert inv.channels[0].a == pytest.approx(12.34)
    assert inv.channels[1].a == pytest.approx(567.8)


async def test_scale_in_block_write_reads_own_scale_factor() -> None:
    # A write through an instance reads that instance's in-block scale factor,
    # so the value is encoded with the per-instance factor.
    class Channel(Component):
        scale_in_block = True
        a = integer(0, scale_register=1, writable=True)

    class Meter(Component):
        channels = repeating_group(2, Channel, stride=2)

    unit = _unit()
    unit.holding.update({1: (-2) & 0xFFFF, 3: (-1) & 0xFFFF})  # ch0 SF -2, ch1 SF -1
    inv = Meter(unit)
    await inv.async_update()
    await inv.channels[1].write("a", 567.8)  # 567.8 / 10**-1 -> raw 5678 at addr 2
    assert (await unit.read_holding_registers(2, 1)) == [5678]


async def test_repeating_group_at_base_offset() -> None:
    # A discovered SunSpec model: the layout is declared relative to the model
    # start and placed with base_offset. The count, the instances and their
    # shared scale register all move with the block; the per-instance shift
    # still leaves the scale register in the shared fixed part.
    class ScaledModule(Component):
        w = integer(11, scale_register=2)

    class Inverter(Component):
        modules = repeating_group(uint16(8), ScaledModule, stride=20)

    unit = _unit()
    unit.holding.update(
        {108: 2, 102: (-2) & 0xFFFF, 111: 1234, 131: 5678}  # everything at +100
    )
    inv = Inverter(unit, base_offset=100)
    await inv.async_update()
    assert inv.modules[0].w == pytest.approx(12.34)  # 1234 * 10**-2
    assert inv.modules[1].w == pytest.approx(56.78)  # 5678 * 10**-2


async def test_static_group_at_base_offset() -> None:
    class Inverter(Component):
        modules = repeating_group(2, Module, stride=2)

    unit = _unit()
    unit.holding.update({110: 1, 111: 11, 112: 2, 113: 22})
    inv = Inverter(unit, base_offset=100)
    await inv.async_update()
    assert [(m.v, m.w) for m in inv.modules] == [(1, 11), (2, 22)]


async def test_write_through_instance_at_base_offset() -> None:
    # a write through an instance lands at block + instance shift
    class WModule(Component):
        setpoint = integer(11, signed=False, writable=True)

    class Inverter(Component):
        modules = repeating_group(2, WModule, stride=20)

    unit = _unit()
    inv = Inverter(unit, base_offset=100)
    await inv.async_update()
    await inv.modules[1].write("setpoint", 42)  # 11 + 100 + 20
    assert (await unit.read_holding_registers(131, 1)) == [42]


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


async def test_nested_static_in_static() -> None:
    # A fixed-count group whose instance itself has a fixed-count group: both
    # levels fold into the parent's read plan, no second pass at all.
    class Inner(Component):
        leaves = repeating_group(2, Module, stride=2)

    class Outer(Component):
        groups = repeating_group(2, Inner, stride=20)

    unit = _unit()
    # outer 0 at +0: leaves at 11/13 ; outer 1 at +20: leaves at 31/33
    unit.holding.update({11: 1, 13: 2, 31: 3, 33: 4})
    outer = Outer(unit)
    await outer.async_update()
    assert [[m.w for m in g.leaves] for g in outer.groups] == [[1, 2], [3, 4]]


async def test_nested_dynamic_in_static() -> None:
    # A fixed-count group whose instance has a register-count group: the inner
    # count is read in the parent's first pass, then the fixed-count instances'
    # second pass sizes and reads the nested register-count groups.
    class Inner(Component):
        leaves = repeating_group(uint16(5), Module, stride=20)

    class Outer(Component):
        groups = repeating_group(2, Inner, stride=100)

    unit = _unit()
    # inner 0 at +0: count 5 = 2 -> leaves at 11/31 ; inner 1 at +100: count 105 = 1
    unit.holding.update({5: 2, 11: 1, 31: 2, 105: 1, 111: 3})
    outer = Outer(unit)
    await outer.async_update()
    assert [[m.w for m in g.leaves] for g in outer.groups] == [[1, 2], [3]]


async def test_nested_dynamic_in_dynamic() -> None:
    # A register-count group whose instance also has a register-count group:
    # each level adds a read pass, the outer count sizing the middle instances
    # whose counts then size the leaves.
    class Inner(Component):
        leaves = repeating_group(uint16(5), Module, stride=20)

    class Outer(Component):
        groups = repeating_group(uint16(0), Inner, stride=100)

    unit = _unit()
    # outer count 0 = 2 ; inner 0 count 5 = 2 -> leaves 11/31 ; inner 1 count 105 = 1
    unit.holding.update({0: 2, 5: 2, 11: 1, 31: 2, 105: 1, 111: 3})
    outer = Outer(unit)
    await outer.async_update()
    assert [[m.w for m in g.leaves] for g in outer.groups] == [[1, 2], [3]]


async def test_nested_dynamic_refreshes_on_recount() -> None:
    # The nested register-count group re-sizes on later polls, inside a
    # fixed-count parent instance.
    class Inner(Component):
        leaves = repeating_group(uint16(5), Module, stride=20)

    class Outer(Component):
        groups = repeating_group(1, Inner, stride=100)

    unit = _unit()
    unit.holding.update({5: 1, 11: 1})
    outer = Outer(unit)
    await outer.async_update()
    assert [m.w for m in outer.groups[0].leaves] == [1]

    unit.holding.update({5: 2, 31: 2})  # device now reports two nested leaves
    await outer.async_update()
    assert [m.w for m in outer.groups[0].leaves] == [1, 2]


def test_factory_validates() -> None:
    with pytest.raises(ValueError, match="stride must be > 0"):
        repeating_group(uint16(8), Module, stride=0)
    with pytest.raises(ValueError, match="must be >= 0"):
        repeating_group(-1, Module, stride=20)
