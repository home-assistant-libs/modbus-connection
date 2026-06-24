"""Tests for the device-modelling framework (modbus_connection.model)."""

from __future__ import annotations

import struct

import pytest

from modbus_connection.mock import MockModbusConnection, MockModbusUnit
from modbus_connection.model import (
    Component,
    Device,
    coil,
    float32,
    gauge,
    int32,
    integer,
    raw_register,
    scaled_sum,
    uint32,
)
from modbus_connection.model import _plan_blocks as plan_blocks


class Meter(Component):
    """A throwaway component exercising every generic field type."""

    count = integer(0, signed=False, writable=True)  # plain uint16
    temperature = gauge(1, 0.1, nan=0x7FFF, unit="°C")  # scaled, with NaN sentinel
    raw_flags = raw_register(2)
    energy = uint32(3, unit="Wh", writable=True)
    balance = int32(5)
    flow = float32(7, unit="m³/h")
    relay = coil(0, writable=True)


def _meter(values: dict[int, int], coils: dict[int, bool] | None = None) -> Meter:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update(values)
    if coils:
        unit.coils.update(coils)
    return Meter(unit)


# -- decode -------------------------------------------------------------------


async def test_scaled_and_raw_and_signed() -> None:
    meter = _meter({0: 1234, 1: 0x10000 - 50, 2: 0xBEEF})
    await meter.async_update()
    assert meter.count == 1234
    assert meter.temperature == pytest.approx(-5.0)  # signed, 0.1
    assert meter.raw_flags == 0xBEEF


async def test_nan_sentinel() -> None:
    meter = _meter({1: 0x7FFF})
    await meter.async_update()
    assert meter.temperature is None


async def test_uint32_int32() -> None:
    raw = (-12345) & 0xFFFFFFFF
    meter = _meter({3: 0x0001, 4: 0x86A0, 5: raw >> 16, 6: raw & 0xFFFF})
    await meter.async_update()
    assert meter.energy == 100000
    assert meter.balance == -12345


async def test_float32() -> None:
    hi, lo = struct.unpack(">HH", struct.pack(">f", 3.14))
    meter = _meter({7: hi, 8: lo})
    await meter.async_update()
    assert meter.flow == pytest.approx(3.14, rel=1e-6)


async def test_word_order_little() -> None:
    class LE(Component):
        value = uint32(0, word_order="little")

    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 0x86A0, 1: 0x0001})  # low word first -> 100000
    le = LE(unit)
    await le.async_update()
    assert le.value == 100000


async def test_plan_is_built_once_across_polls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import modbus_connection.model as model

    calls = 0
    original = model._plan_blocks

    def counting(*args: object, **kwargs: object) -> list[tuple[int, int]]:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(model, "_plan_blocks", counting)
    meter = _meter({0: 7})
    await meter.async_update()
    await meter.async_update()
    await meter.async_update()
    # One plan for registers + one for coils, regardless of how many polls run.
    assert calls == 2


async def test_scaled_sum_adds_magnitudes() -> None:
    class Energy(Component):
        total = scaled_sum(0, (1, 1000, 1_000_000))  # Wh, kWh, MWh

    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 3, 1: 2, 2: 1})  # 3 Wh + 2 kWh + 1 MWh
    energy = Energy(unit)
    await energy.async_update()
    assert energy.total == 1_002_003


async def test_dynamic_scale_register() -> None:
    class Scaled(Component):
        current = gauge(0, 1.0, signed=False, scale_register=1)

    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 1234, 1: (-2) & 0xFFFF})  # 1234 * 10**-2
    scaled = Scaled(unit)
    await scaled.async_update()
    assert scaled.current == pytest.approx(12.34)


async def test_dynamic_scale_register_pooled_in_one_read() -> None:
    class Scaled(Component):
        current = gauge(0, 1.0, signed=False, scale_register=2)

    reads: list[tuple[int, int]] = []

    class Counting:
        def __init__(self, inner: MockModbusUnit) -> None:
            self._inner = inner

        async def read_holding_registers(self, address: int, count: int) -> list[int]:
            reads.append((address, count))
            return await self._inner.read_holding_registers(address, count)

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

    inner = MockModbusConnection().for_unit(1)
    inner.holding.update({0: 1234, 2: 0})  # value at 0, scale factor at 2
    scaled = Scaled(Counting(inner))  # type: ignore[arg-type]
    await scaled.async_update()
    # Value (0) and its scale register (2) sit close enough to share one block.
    assert len(reads) == 1
    assert scaled.current == pytest.approx(1234.0)


# -- writes -------------------------------------------------------------------


async def test_write_register_and_coil() -> None:
    meter = _meter({})
    await meter.write("count", 4242)
    await meter.write("relay", True)
    await meter.async_update()
    assert meter.count == 4242
    assert meter.relay is True


async def test_write_multi_register() -> None:
    meter = _meter({})
    await meter.write("energy", 100000)
    await meter.async_update()
    assert meter.energy == 100000


async def test_write_rejects_readonly() -> None:
    meter = _meter({})
    with pytest.raises(AttributeError):
        await meter.write("temperature", 20.0)


async def test_write_unlock_coil() -> None:
    class Locked(Component):
        mode = integer(10, writable=True, level_coil=88)

    unit = MockModbusConnection().for_unit(1)
    await unit.write_coil(88, True)  # start locked
    locked = Locked(unit)
    await locked.write("mode", 3)
    # The unlock coil was released to False, then the value written.
    assert (await unit.read_coils(88, 1))[0] is False
    assert (await unit.read_holding_registers(10, 1))[0] == 3


# -- listeners + independent update ------------------------------------------


async def test_listeners_and_independent_update() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 7})
    a = Meter(unit)
    b = Meter(unit)
    calls: list[int] = []
    unsubscribe = a.add_update_listener(lambda: calls.append(1))

    await a.async_update()
    assert a.count == 7 and len(calls) == 1
    assert b.count is None  # b refreshes independently

    unsubscribe()
    await a.async_update()
    assert len(calls) == 1  # no longer notified


# -- block planning -----------------------------------------------------------


def test_plan_blocks_gap_based() -> None:
    # Addresses within _MAX_GAP merge; a wider gap splits.
    blocks = plan_blocks([(0, 1), (3, 1), (20, 1)])
    assert blocks == [(0, 4), (20, 1)]


def test_plan_blocks_keeps_multiregister_whole() -> None:
    blocks = plan_blocks([(a, 1) for a in range(99)] + [(99, 2)])
    field_block = next(b for b in blocks if b[0] <= 99 < b[0] + b[1])
    assert field_block[0] <= 100 < field_block[0] + field_block[1]


def test_plan_blocks_range_aware_never_crosses_gap() -> None:
    ranges = ((0, 6), (9, 40))  # 7-8 unreadable
    blocks = plan_blocks([(5, 1), (9, 1), (12, 1)], ranges)
    read = {start + i for start, count in blocks for i in range(count)}
    assert 7 not in read and 8 not in read
    # 9 and 12 are in the same range -> one block (merged across the small gap).
    assert (9, 4) in blocks


# -- device-level pooling -----------------------------------------------------


class _Counting:
    """Wraps a unit and records each holding-register read."""

    def __init__(self, inner: MockModbusUnit) -> None:
        self._inner = inner
        self.reads: list[tuple[int, int]] = []

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        self.reads.append((address, count))
        return await self._inner.read_holding_registers(address, count)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


async def test_device_pools_reads() -> None:
    inner = MockModbusConnection().for_unit(1)
    inner.holding.update({0: 1, 1: 200, 3: 0x0001, 4: 0x86A0})
    unit = _Counting(inner)
    meter = Meter(unit)  # type: ignore[arg-type]

    device = Device(unit, [meter])  # type: ignore[list-item]
    await device.async_update()

    # count/temperature/raw/energy/balance/flow span 0..8 -> one pooled block.
    assert len(unit.reads) == 1
    assert meter.count == 1 and meter.energy == 100000


async def test_device_reuses_plan_across_polls() -> None:
    inner = MockModbusConnection().for_unit(1)
    inner.holding.update({0: 1, 3: 0x0001, 4: 0x86A0})
    unit = _Counting(inner)
    device = Device(unit, [Meter(unit)])  # type: ignore[list-item]

    await device.async_update()
    await device.async_update()
    # Same single pooled block each poll: 2 reads total, no re-planning surprises.
    assert unit.reads == [unit.reads[0], unit.reads[0]]
    assert len(unit.reads) == 2
