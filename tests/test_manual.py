"""Tests for ManualComponent — the imperative, runtime-built read/write group."""

from __future__ import annotations

import pytest

from modbus_connection.exceptions import ModbusExceptionError
from modbus_connection.mock import MockModbusConnection, MockModbusUnit
from modbus_connection.model import (
    Component,
    ManualComponent,
    coil,
    discrete_input,
    gauge,
    integer,
    repeating_group,
    uint32,
)
from modbus_connection.model.sunspec import uint16


def _unit() -> MockModbusUnit:
    return MockModbusConnection().for_unit(1)


class _Module(Component):
    """One repeating sub-unit, at instance 0's addresses."""

    w = integer(11, signed=False)


class _Module2(Component):
    x = integer(50, signed=False)


class _Spy:
    """Wraps a unit and records every read and write call."""

    def __init__(self, inner: MockModbusUnit) -> None:
        self._inner = inner
        self.reads: list[tuple[str, int, int]] = []
        self.writes: list[tuple[str, int, object]] = []

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        self.reads.append(("holding", address, count))
        return await self._inner.read_holding_registers(address, count)

    async def read_input_registers(self, address: int, count: int) -> list[int]:
        self.reads.append(("input", address, count))
        return await self._inner.read_input_registers(address, count)

    async def read_coils(self, address: int, count: int) -> list[bool]:
        self.reads.append(("coil", address, count))
        return await self._inner.read_coils(address, count)

    async def read_discrete_inputs(self, address: int, count: int) -> list[bool]:
        self.reads.append(("discrete", address, count))
        return await self._inner.read_discrete_inputs(address, count)

    async def write_register(self, address: int, value: int) -> None:
        self.writes.append(("fc06", address, value))
        await self._inner.write_register(address, value)

    async def write_registers(self, address: int, values: list[int]) -> None:
        self.writes.append(("fc16", address, values))
        await self._inner.write_registers(address, values)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


async def test_mixes_all_four_tables() -> None:
    unit = _unit()
    unit.holding.update({40: 215, 2: 0x0001, 3: 0x86A0})
    unit.input.update({5: 555, 6: 777})
    unit.coils.update({5: True})
    unit.discrete_inputs.update({9: True})

    mc = ManualComponent(unit)
    mc.add("temp", gauge(40, 0.1))  # holding (default)
    mc.add("energy", uint32(2))  # holding
    mc.add("flow", uint32(5), space="input")  # input registers
    mc.add("relay", coil(5))  # coils (FC01)
    mc.add("alarm", discrete_input(9))  # discrete inputs (FC02)

    data = await mc.async_update()
    assert data == {
        "temp": pytest.approx(21.5),
        "energy": 100000,
        "flow": (555 << 16) | 777,
        "relay": True,
        "alarm": True,
    }
    assert mc.get("relay") is True
    assert mc.values["temp"] == pytest.approx(21.5)


async def test_pools_adjacent_registers_but_keeps_tables_apart() -> None:
    inner = _unit()
    inner.holding.update({0: 1, 4: 2})  # within max_gap -> one read
    inner.input.update({0: 3})
    inner.coils.update({0: True})
    inner.discrete_inputs.update({0: False})
    unit = _Spy(inner)

    mc = ManualComponent(unit)  # type: ignore[arg-type]
    mc.add("a", integer(0))
    mc.add("b", integer(4))
    mc.add("c", integer(0), space="input")
    mc.add("relay", coil(0))
    mc.add("di", discrete_input(0))
    await mc.async_update()

    # holding 0 and 4 pooled into one read; each other table read separately,
    # even though they share address 0.
    assert ("holding", 0, 5) in unit.reads
    assert ("input", 0, 1) in unit.reads
    assert ("coil", 0, 1) in unit.reads
    assert ("discrete", 0, 1) in unit.reads
    assert mc.get("a") == 1 and mc.get("b") == 2 and mc.get("c") == 3


async def test_max_gap_override_merges_wider() -> None:
    inner = _unit()
    inner.holding.update({0: 1, 20: 2})
    unit = _Spy(inner)
    mc = ManualComponent(unit, max_gap=24)  # type: ignore[arg-type]
    mc.add("a", integer(0))
    mc.add("b", integer(20))
    await mc.async_update()
    assert ("holding", 0, 21) in unit.reads  # 20 apart, merged thanks to max_gap=24


async def test_ranges_keep_reads_within_a_table() -> None:
    inner = _unit()
    inner.holding.update({0: 1, 9: 2})  # 7-8 unreadable per the ranges
    inner.coils.update({0: True, 9: False})
    unit = _Spy(inner)
    mc = ManualComponent(  # type: ignore[arg-type]
        unit, holding_ranges=((0, 6), (9, 40)), coil_ranges=((0, 6), (9, 40))
    )
    mc.add("near", integer(0))
    mc.add("far", integer(9))  # without ranges, max_gap would merge 0..9
    mc.add("c0", coil(0))
    mc.add("c9", coil(9))
    await mc.async_update()
    read = {(fc, start + i) for fc, start, count in unit.reads for i in range(count)}
    assert ("holding", 7) not in read and ("holding", 8) not in read
    assert ("coil", 7) not in read and ("coil", 8) not in read
    assert mc.get("near") == 1 and mc.get("far") == 2 and mc.get("c9") is False


async def test_dynamic_scale_register() -> None:
    unit = _unit()
    unit.holding.update({0: 1234, 5: (-2) & 0xFFFF})  # sunssf -2 -> 10**-2
    mc = ManualComponent(unit)
    mc.add("current", uint16(0, scale_register=5))
    mc.add("a_sf", uint16(5))
    await mc.async_update()
    assert mc.get("current") == pytest.approx(12.34)


async def test_write_register_and_coil() -> None:
    unit = _unit()
    mc = ManualComponent(unit)
    mc.add("setpoint", gauge(10, 0.1, writable=True))
    mc.add("relay", coil(5, writable=True))
    await mc.write("setpoint", 21.5)
    await mc.write("relay", True)
    assert (await unit.read_holding_registers(10, 1)) == [215]
    assert (await unit.read_coils(5, 1)) == [True]


async def test_write_force_fc16_and_validator() -> None:
    inner = _unit()
    unit = _Spy(inner)
    # force_fc16: a single-register write still goes out as FC16.
    mc = ManualComponent(unit)  # type: ignore[arg-type]
    mc.add("reg", integer(3, writable=True, force_fc16=True))
    # validator: vets/coerces the value before it is written.
    mc.add("vetted", integer(4, writable=lambda v: max(0, int(v))))
    await mc.write("reg", 7)
    await mc.write("vetted", -5)  # coerced to 0
    assert ("fc16", 3, [7]) in unit.writes
    assert (await inner.read_holding_registers(4, 1)) == [0]


async def test_write_validator_can_reject() -> None:
    unit = _unit()

    def positive(v: int) -> int:
        if v < 0:
            raise ValueError("must be >= 0")
        return v

    mc = ManualComponent(unit)
    mc.add("reg", integer(0, writable=positive))
    with pytest.raises(ValueError, match=">= 0"):
        await mc.write("reg", -1)


async def test_write_rejections() -> None:
    unit = _unit()
    mc = ManualComponent(unit)
    mc.add("ro", integer(0))  # not writable
    mc.add("inp", integer(1, writable=True), space="input")  # input is read-only
    mc.add("di", discrete_input(2))  # discrete inputs are read-only
    for key, match in [("ro", "read-only"), ("inp", "input"), ("di", "read-only")]:
        with pytest.raises(AttributeError, match=match):
            await mc.write(key, 1)
    with pytest.raises(AttributeError, match="unknown key"):
        await mc.write("nope", 1)


async def test_add_and_remove_invalidate_plan() -> None:
    unit = _unit()
    unit.holding.update({0: 1, 1: 2})
    mc = ManualComponent(unit)
    mc.add("a", integer(0))
    assert await mc.async_update() == {"a": 1}

    mc.add("b", integer(1))  # invalidates the cached plan
    assert await mc.async_update() == {"a": 1, "b": 2}

    mc.remove("a")  # invalidates again and drops the value
    assert await mc.async_update() == {"b": 2}
    assert mc.get("a") is None


async def test_block_exception_sets_covered_keys_none() -> None:
    unit = _unit()

    def boom() -> int:
        raise ModbusExceptionError(2)  # illegal data address

    unit.holding[0] = boom
    unit.input[0] = 7
    mc = ManualComponent(unit)
    mc.add("bad", integer(0))  # its holding block raises
    mc.add("good", integer(0), space="input")  # a different table still reads
    await mc.async_update()
    assert mc.get("bad") is None
    assert mc.get("good") == 7


async def test_listeners_fire_on_update() -> None:
    unit = _unit()
    unit.holding[0] = 5
    mc = ManualComponent(unit)
    mc.add("a", integer(0))
    calls: list[int] = []
    unsub = mc.add_update_listener(lambda: calls.append(1))
    await mc.async_update()
    assert calls == [1]
    unsub()
    await mc.async_update()
    assert calls == [1]  # no longer notified


async def test_update_notifies_group_sub_instances() -> None:
    unit = _unit()
    unit.holding.update({8: 2, 11: 100, 31: 95})  # count@8=2; two modules
    mc = ManualComponent(unit)
    mc.add("modules", repeating_group(uint16(8), _Module, stride=20))
    await mc.async_update()  # size the group so its instances exist

    calls: list[int] = []
    for module in mc.get("modules"):
        module.add_update_listener(lambda: calls.append(1))
    await mc.async_update()
    assert calls == [1, 1]  # each sub-instance's listener fires on update


def test_add_validates_space_and_type() -> None:
    mc = ManualComponent(_unit())
    with pytest.raises(ValueError, match="register space"):
        mc.add("x", integer(0), space="coil")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="space is fixed"):
        mc.add("y", coil(0), space="coil")
    with pytest.raises(TypeError, match="RegisterField, a bit field or a repeating"):
        mc.add("z", "not a field")  # type: ignore[arg-type]


# -- repeating groups ---------------------------------------------------------


async def test_repeating_group_register_count() -> None:
    unit = _unit()
    unit.holding.update({8: 2, 11: 100, 31: 95})  # count@8=2; module 0 w@11, 1 w@31
    mc = ManualComponent(unit)
    mc.add("modules", repeating_group(uint16(8), _Module, stride=20))
    assert mc.get("modules") == []  # not sized until the first update

    await mc.async_update()
    modules = mc.get("modules")
    assert isinstance(modules[0], _Module)
    assert [m.w for m in modules] == [100, 95]

    unit.holding[8] = 1  # device now reports one module
    await mc.async_update()
    assert [m.w for m in mc.get("modules")] == [100]


async def test_repeating_group_fixed_count_folds_into_read() -> None:
    inner = _unit()
    inner.holding.update({11: 100, 13: 95})  # stride 2 -> module 0 w@11, 1 w@13
    unit = _Spy(inner)
    mc = ManualComponent(unit)  # type: ignore[arg-type]
    mc.add("modules", repeating_group(2, _Module, stride=2))
    await mc.async_update()
    assert [m.w for m in mc.get("modules")] == [100, 95]
    # A fixed count is static, so its instances read in the one pooled block.
    assert unit.reads == [("holding", 11, 3)]


async def test_repeating_group_mixed_with_plain_targets() -> None:
    unit = _unit()
    unit.holding.update({0: 7, 8: 2, 11: 100, 31: 95})
    mc = ManualComponent(unit)
    mc.add("serial", integer(0, signed=False))
    mc.add("modules", repeating_group(uint16(8), _Module, stride=20))
    data = await mc.async_update()
    assert data["serial"] == 7  # plain values still come out in the dict
    assert [m.w for m in mc.get("modules")] == [100, 95]


async def test_adding_a_group_invalidates_the_folded_cache() -> None:
    unit = _unit()
    unit.holding.update({8: 1, 11: 100, 50: 7, 52: 9})
    mc = ManualComponent(unit)
    mc.add("modules", repeating_group(uint16(8), _Module, stride=20))
    await mc.async_update()
    assert [m.w for m in mc.get("modules")] == [100]

    # Add a fixed-count group after the first update: its instances must fold into
    # the rebuilt plan, not be lost to the cached (empty) static-items list.
    mc.add("extra", repeating_group(2, _Module2, stride=2))
    await mc.async_update()
    assert [m.x for m in mc.get("extra")] == [7, 9]
    assert [m.w for m in mc.get("modules")] == [100]


async def test_add_replaces_a_group_with_a_plain_register() -> None:
    # Re-adding a key clears whatever was there — a group must not linger when the
    # key is reused for a plain register (add() removes the key first).
    unit = _unit()
    unit.holding[5] = 42
    mc = ManualComponent(unit)
    mc.add("x", repeating_group(uint16(8), _Module, stride=20))
    mc.add("x", integer(5))  # same key, now a plain register
    data = await mc.async_update()
    assert data["x"] == 42  # read as a register
    assert mc.get("x") == 42  # no leftover group instances
