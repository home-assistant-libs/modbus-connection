"""Tests for read_repeating — repeated sub-blocks counted at poll time."""

from __future__ import annotations

import pytest

from modbus_connection.mock import MockModbusConnection, MockModbusUnit
from modbus_connection.model import coil, integer, read_repeating
from modbus_connection.model.sunspec import uint16


def _unit() -> MockModbusUnit:
    return MockModbusConnection().for_unit(1)


class _Spy:
    """Wraps a unit and records (function, address, count) for every read."""

    def __init__(self, inner: MockModbusUnit) -> None:
        self._inner = inner
        self.reads: list[tuple[str, int, int]] = []

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        self.reads.append(("holding", address, count))
        return await self._inner.read_holding_registers(address, count)

    async def read_coils(self, address: int, count: int) -> list[bool]:
        self.reads.append(("coil", address, count))
        return await self._inner.read_coils(address, count)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


async def test_count_register_sizes_the_instances() -> None:
    unit = _unit()
    # count=2 at addr 0; block "w" at 10 (stride 5) -> 10, 15; "v" at 11 -> 11, 16
    unit.holding.update({0: 2, 10: 100, 11: 220, 15: 95, 16: 210})
    instances = await read_repeating(
        unit,
        count=uint16(0),
        block={"w": integer(10, stride=5), "v": integer(11, stride=5)},
    )
    assert instances == [{"w": 100, "v": 220}, {"w": 95, "v": 210}]
    assert len(instances) == 2  # count is just len()


async def test_count_can_change_between_calls() -> None:
    unit = _unit()
    unit.holding.update({0: 1, 10: 1, 15: 2, 20: 3})
    block = {"w": integer(10, stride=5)}

    assert await read_repeating(unit, count=uint16(0), block=block) == [{"w": 1}]

    unit.holding[0] = 3  # device now reports three instances
    assert await read_repeating(unit, count=uint16(0), block=block) == [
        {"w": 1},
        {"w": 2},
        {"w": 3},
    ]


async def test_unimplemented_count_yields_no_instances() -> None:
    unit = _unit()
    unit.holding[0] = 0xFFFF  # uint16 unimplemented -> None -> 0 instances
    assert await read_repeating(unit, count=uint16(0), block={"w": integer(10)}) == []


async def test_fixed_count_reads_in_one_round_trip() -> None:
    inner = _unit()
    inner.holding.update({10: 1, 11: 2})  # two instances, adjacent -> one pooled read
    unit = _Spy(inner)
    instances = await read_repeating(unit, count=2, block={"w": integer(10, stride=1)})  # type: ignore[arg-type]
    assert instances == [{"w": 1}, {"w": 2}]
    # A fixed count needs no count read, so a single pooled holding read suffices.
    assert unit.reads == [("holding", 10, 2)]


async def test_register_count_costs_one_extra_read() -> None:
    inner = _unit()
    inner.holding.update({0: 2, 10: 1, 11: 2})
    unit = _Spy(inner)
    await read_repeating(unit, count=uint16(0), block={"w": integer(10, stride=1)})  # type: ignore[arg-type]
    # Read the count, then read count+instances once sized.
    assert len(unit.reads) == 2


async def test_per_instance_scale_register() -> None:
    unit = _unit()
    # Each instance scales off its own sunssf: inst0 sf at 2 = -2, inst1 sf at 4 = -1
    unit.holding.update({0: 2, 2: (-2) & 0xFFFF, 4: (-1) & 0xFFFF, 10: 1234, 15: 5678})
    instances = await read_repeating(
        unit,
        count=uint16(0),
        block={"w": uint16(10, scale_register=2, scale_register_stride=2, stride=5)},
    )
    assert instances[0]["w"] == pytest.approx(12.34)  # 1234 * 10**-2
    assert instances[1]["w"] == pytest.approx(567.8)  # 5678 * 10**-1


async def test_coil_block() -> None:
    unit = _unit()
    unit.holding[0] = 2
    unit.coils.update({5: True, 6: False})
    instances = await read_repeating(
        unit, count=uint16(0), block={"on": coil(5, stride=1)}
    )
    assert instances == [{"on": True}, {"on": False}]


async def test_template_fields_are_not_mutated() -> None:
    # The same block field reused across instances must not be rewritten in place.
    unit = _unit()
    unit.holding.update({0: 2, 10: 1, 15: 2})
    w = integer(10, stride=5)
    await read_repeating(unit, count=uint16(0), block={"w": w})
    assert w.address == 10  # untouched: instances are addressed via copies
    assert w.stride == 5
