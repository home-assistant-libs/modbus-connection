"""Tests for the device-modelling framework (modbus_connection.model)."""

from __future__ import annotations

import struct
from enum import IntEnum, IntFlag

import pytest

from modbus_connection.mock import MockModbusConnection, MockModbusUnit
from modbus_connection.model import (
    Component,
    ComponentGroup,
    coil,
    enum,
    flags,
    float32,
    float64,
    gauge,
    int32,
    integer,
    raw_register,
    string,
    uint32,
    uint64,
)
from modbus_connection.model._planning import _plan_blocks as plan_blocks
from modbus_connection.model.fields import (
    FloatField,
    IPv4Field,
    NumberField,
    RawField,
)


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


async def test_fractional_scale_above_one_rounds_not_truncates() -> None:
    class Dev(Component):
        value = gauge(0, 2.5)  # 3 * 2.5 = 7.5, must not truncate to 7

    unit = MockModbusConnection().for_unit(1)
    unit.holding[0] = 3
    dev = Dev(unit)
    await dev.async_update()
    assert dev.value == pytest.approx(7.5)


async def test_write_out_of_range_raises() -> None:
    meter = _meter({})
    with pytest.raises(OverflowError):
        await meter.write("count", 70000)  # count is a uint16


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


async def test_byte_order_little() -> None:
    class Swapped(Component):
        reg16 = integer(0, signed=False, byte_order="little", writable=True)
        reg32 = uint32(1, byte_order="little")

    unit = MockModbusConnection().for_unit(1)
    # Bytes swapped within each register: 0x3412 -> 0x1234, 0x3412/0x7856 -> 0x12345678
    unit.holding.update({0: 0x3412, 1: 0x3412, 2: 0x7856})
    dev = Swapped(unit)
    await dev.async_update()
    assert dev.reg16 == 0x1234
    assert dev.reg32 == 0x12345678

    # A write byte-swaps on the way back out.
    await dev.write("reg16", 0x1234)
    assert unit.holding[0] == 0x3412


async def test_plan_is_built_once_across_polls() -> None:
    meter = _meter({0: 7})
    await meter.async_update()
    register_blocks = meter._register_blocks
    coil_blocks = meter._coil_blocks
    await meter.async_update()
    await meter.async_update()
    # The cached_property plan is the same object each poll, never rebuilt.
    assert meter._register_blocks is register_blocks
    assert meter._coil_blocks is coil_blocks


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


# -- field types --------------------------------------------------------------


def test_factories_return_concrete_field_types() -> None:
    assert isinstance(gauge(0, 0.1), NumberField)
    assert isinstance(integer(0), NumberField)
    assert isinstance(uint32(0), NumberField)
    assert isinstance(int32(0), NumberField)
    assert isinstance(float32(0), FloatField)
    assert isinstance(raw_register(0), RawField)


def test_read_only_field_encode_raises() -> None:
    with pytest.raises(NotImplementedError):
        IPv4Field(0, count=2).encode(5)  # an address field is read-only


def test_unbound_field_unknown_enum_decodes_to_none() -> None:
    class Mode(IntEnum):
        OFF = 0

    # A field never assigned to a Component (no __set_name__) must still decode an
    # unknown enum code to None rather than crash on the warning path.
    assert enum(0, Mode).decode([9]) is None


async def test_generic_enum_flags_string_and_64bit() -> None:
    class Mode(IntEnum):
        OFF = 0
        HEAT = 2

    class Events(IntFlag):
        A = 1
        B = 2

    class Dev(Component):
        mode = enum(0, Mode)
        events = flags(1, Events)
        name = string(2, 2)  # "ABCD"
        total = uint64(4)
        ratio = float64(8)

    unit = MockModbusConnection().for_unit(1)
    hi = struct.unpack(">HHHH", struct.pack(">d", 1.5))
    unit.holding.update({0: 2, 1: 0b11, 2: 0x4142, 3: 0x4344, 4: 0, 5: 0, 6: 0, 7: 5})
    unit.holding.update(dict(zip(range(8, 12), hi, strict=True)))
    dev = Dev(unit)
    await dev.async_update()
    assert dev.mode is Mode.HEAT
    assert dev.events == Events.A | Events.B
    assert dev.name == "ABCD"
    assert dev.total == 5
    assert dev.ratio == pytest.approx(1.5)


async def test_generic_enum_signed_codes() -> None:
    class Mode(IntEnum):
        ERR = -1  # sent as 0xFFFF
        OK = 0

    class Dev(Component):
        signed_mode = enum(0, Mode, signed=True)
        unsigned_mode = enum(1, Mode)  # default unsigned

    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 0xFFFF, 1: 0xFFFF})
    dev = Dev(unit)
    await dev.async_update()
    assert dev.signed_mode is Mode.ERR  # 0xFFFF read as -1
    assert dev.unsigned_mode is None  # 65535 has no member


async def test_generic_enum_unknown_value_is_none() -> None:
    from modbus_connection.model import fields

    class Mode(IntEnum):
        OFF = 0

    class Dev(Component):
        mode = enum(0, Mode)

    fields._warned_unknown_enum.clear()
    unit = MockModbusConnection().for_unit(1)
    unit.holding[0] = 9  # not a Mode member
    dev = Dev(unit)
    await dev.async_update()
    assert dev.mode is None


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


def test_plan_blocks_rejects_field_wider_than_read_limit() -> None:
    with pytest.raises(ValueError, match="exceeds"):
        plan_blocks([(0, 130)])  # one value can't span >125 registers in a read


def test_plan_blocks_range_aware_never_crosses_gap() -> None:
    ranges = ((0, 6), (9, 40))  # 7-8 unreadable
    blocks = plan_blocks([(5, 1), (9, 1), (12, 1)], ranges)
    read = {start + i for start, count in blocks for i in range(count)}
    assert 7 not in read and 8 not in read
    # 9 and 12 are in the same range -> one block (merged across the small gap).
    assert (9, 4) in blocks


def test_plan_blocks_configurable_max_gap() -> None:
    spans = [(0, 1), (10, 1)]  # 10 apart
    assert plan_blocks(spans, max_gap=8) == [(0, 1), (10, 1)]  # gap too wide -> split
    assert plan_blocks(spans, max_gap=16) == [(0, 11)]  # within gap -> one read


def test_plan_blocks_configurable_max_span() -> None:
    # With the gap allowing a merge, max_span decides whether the block is too wide.
    spans = [(0, 1), (40, 1)]
    assert plan_blocks(spans, max_gap=50, max_span=30) == [(0, 1), (40, 1)]  # 41 > 30
    assert plan_blocks(spans, max_gap=50, max_span=60) == [(0, 41)]  # 41 <= 60


async def test_component_max_gap_override_changes_plan() -> None:
    class Wide(Component):
        max_gap = 20
        a = integer(0)
        b = integer(10)  # 10 away from a

    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 1, 10: 2})
    comp = Wide(unit)
    await comp.async_update()
    # With max_gap=20 the two fields merge into one block read (0..10).
    assert comp._register_blocks["holding"] == [(0, 11)]
    assert comp.a == 1 and comp.b == 2


async def test_group_rejects_mismatched_max_gap() -> None:
    class A(Component):
        max_gap = 8
        x = integer(0)

    class B(Component):
        max_gap = 16
        y = integer(0)

    unit = MockModbusConnection().for_unit(1)
    with pytest.raises(ValueError, match="max_gap"):
        ComponentGroup(unit, [A(unit), B(unit)])


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


async def test_group_pools_reads() -> None:
    inner = MockModbusConnection().for_unit(1)
    inner.holding.update({0: 1, 1: 200, 3: 0x0001, 4: 0x86A0})
    unit = _Counting(inner)
    meter = Meter(unit)  # type: ignore[arg-type]

    group = ComponentGroup(unit, [meter])  # type: ignore[list-item]
    await group.async_update()

    # count/temperature/raw/energy/balance/flow span 0..8 -> one pooled block.
    assert len(unit.reads) == 1
    assert meter.count == 1 and meter.energy == 100000


async def test_group_reuses_plan_across_polls() -> None:
    inner = MockModbusConnection().for_unit(1)
    inner.holding.update({0: 1, 3: 0x0001, 4: 0x86A0})
    unit = _Counting(inner)
    group = ComponentGroup(unit, [Meter(unit)])  # type: ignore[list-item]

    await group.async_update()
    await group.async_update()
    # Same single pooled block each poll: 2 reads total, no re-planning surprises.
    assert unit.reads == [unit.reads[0], unit.reads[0]]
    assert len(unit.reads) == 2


class _Ranged(Component):
    register_ranges = ((0, 6), (9, 40))  # 7-8 unreadable
    near = integer(5)
    far = integer(9)


async def test_group_derives_ranges_from_components() -> None:
    inner = MockModbusConnection().for_unit(1)
    inner.holding.update({5: 1, 9: 2})
    unit = _Counting(inner)
    # Two components sharing ranges: accepted, and the gap is honoured.
    group = ComponentGroup(unit, [_Ranged(unit), _Ranged(unit)])  # type: ignore[list-item]
    await group.async_update()
    read = {start + i for start, count in unit.reads for i in range(count)}
    assert 7 not in read and 8 not in read  # never crosses the unreadable gap


async def test_group_rejects_mismatched_ranges() -> None:
    class Other(Component):
        register_ranges = ((0, 40),)
        value = integer(0)

    unit = MockModbusConnection().for_unit(1)
    with pytest.raises(ValueError, match="register_ranges"):
        ComponentGroup(unit, [_Ranged(unit), Other(unit)])


# -- input registers (FC04) ---------------------------------------------------


class _SpyUnit:
    """Records ``(space, address, count)`` for both register read functions."""

    def __init__(self, inner: MockModbusUnit) -> None:
        self._inner = inner
        self.reads: list[tuple[str, int, int]] = []

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        self.reads.append(("holding", address, count))
        return await self._inner.read_holding_registers(address, count)

    async def read_input_registers(self, address: int, count: int) -> list[int]:
        self.reads.append(("input", address, count))
        return await self._inner.read_input_registers(address, count)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


class _InputMeter(Component):
    register_space = "input"
    temp = gauge(5, 0.1)


class _HoldingMeter(Component):
    power = integer(0, signed=False)


async def test_input_component_reads_via_fc04() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.input[5] = 215
    unit.holding[5] = 999  # would decode to 99.9 if (wrongly) read from holding
    meter = _InputMeter(unit)
    await meter.async_update()
    assert meter.temp == pytest.approx(21.5)


async def test_group_reads_input_and_holding_separately() -> None:
    inner = MockModbusConnection().for_unit(1)
    inner.holding[0] = 100
    inner.input[5] = 215
    unit = _SpyUnit(inner)
    holding, inp = _HoldingMeter(unit), _InputMeter(unit)  # type: ignore[arg-type]
    await ComponentGroup(unit, [holding, inp]).async_update()  # type: ignore[list-item]
    assert holding.power == 100
    assert inp.temp == pytest.approx(21.5)
    assert ("holding", 0, 1) in unit.reads
    assert ("input", 5, 1) in unit.reads


async def test_adjacent_input_and_holding_not_merged() -> None:
    class _InputAt5(Component):
        register_space = "input"
        a = integer(5)

    class _HoldingAt6(Component):
        b = integer(6)

    inner = MockModbusConnection().for_unit(1)
    inner.input[5] = 1
    inner.holding[6] = 2
    unit = _SpyUnit(inner)
    await ComponentGroup(  # type: ignore[list-item]
        unit, [_InputAt5(unit), _HoldingAt6(unit)]
    ).async_update()
    # Numerically adjacent but in different spaces: two separate single-word reads.
    assert ("input", 5, 1) in unit.reads
    assert ("holding", 6, 1) in unit.reads


async def test_input_component_respects_ranges() -> None:
    class _RangedInput(Component):
        register_space = "input"
        register_ranges = ((0, 6), (9, 40))  # 7-8 unreadable
        near = integer(5)
        far = integer(9)

    inner = MockModbusConnection().for_unit(1)
    inner.input.update({5: 1, 9: 2})
    unit = _SpyUnit(inner)
    comp = _RangedInput(unit)  # type: ignore[arg-type]
    await comp.async_update()
    read = {
        (space, start + i) for space, start, count in unit.reads for i in range(count)
    }
    assert ("input", 7) not in read and ("input", 8) not in read
    assert comp.near == 1 and comp.far == 2


async def test_group_allows_different_ranges_across_spaces() -> None:
    class InputComp(Component):
        register_space = "input"
        register_ranges = ((0, 50),)
        a = integer(0)

    class HoldingComp(Component):
        register_ranges = ((0, 100),)
        b = integer(0)

    unit = MockModbusConnection().for_unit(1)
    # Different ranges are fine because the components are in different spaces.
    ComponentGroup(unit, [InputComp(unit), HoldingComp(unit)])


async def test_group_rejects_mismatched_ranges_within_a_space() -> None:
    class InputA(Component):
        register_space = "input"
        register_ranges = ((0, 50),)
        a = integer(0)

    class InputB(Component):
        register_space = "input"
        register_ranges = ((0, 99),)
        b = integer(0)

    unit = MockModbusConnection().for_unit(1)
    with pytest.raises(ValueError, match="input-space"):
        ComponentGroup(unit, [InputA(unit), InputB(unit)])


async def test_write_to_input_field_raises() -> None:
    class WritableInput(Component):
        register_space = "input"
        x = integer(0, writable=True)

    unit = MockModbusConnection().for_unit(1)
    with pytest.raises(AttributeError, match="input"):
        await WritableInput(unit).write("x", 5)
