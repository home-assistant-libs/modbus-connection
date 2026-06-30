"""Tests for the device-modelling framework (modbus_connection.model)."""

from __future__ import annotations

import struct
from collections.abc import Callable
from enum import IntEnum, IntFlag
from typing import Any

import pytest

from modbus_connection.decode import decode_float32
from modbus_connection.exceptions import ModbusExceptionError
from modbus_connection.mock import MockModbusConnection, MockModbusUnit
from modbus_connection.model import (
    Component,
    ComponentGroup,
    coil,
    discrete_input,
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
    CoilField,
    DiscreteInputField,
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


async def test_affine_offset_decode() -> None:
    """A scaled field decodes as ``raw * scale + offset`` (affine read)."""

    class Dev(Component):
        temp = gauge(0, 0.1, offset=-100.0)  # 1500 * 0.1 - 100 = 50.0

    unit = MockModbusConnection().for_unit(1)
    unit.holding[0] = 1500
    dev = Dev(unit)
    await dev.async_update()
    assert dev.temp == pytest.approx(50.0)


async def test_integer_offset_stays_integral() -> None:
    """An offset on an unscaled integer shifts the value but keeps it an int."""

    class Dev(Component):
        shifted = integer(0, offset=-100)  # 105 - 100 = 5

    unit = MockModbusConnection().for_unit(1)
    unit.holding[0] = 105
    dev = Dev(unit)
    await dev.async_update()
    assert dev.shifted == 5
    assert isinstance(dev.shifted, int)


async def test_offset_keeps_scale_decimals() -> None:
    """A whole-number offset must not coarsen a fractional scale's rounding."""

    class Dev(Component):
        temp = gauge(0, 0.1, offset=-100)  # 1234 * 0.1 - 100 = 23.4, keep the .4

    unit = MockModbusConnection().for_unit(1)
    unit.holding[0] = 1234
    dev = Dev(unit)
    await dev.async_update()
    assert dev.temp == pytest.approx(23.4)


async def test_affine_offset_round_trips_on_write() -> None:
    """Writing inverts the affine map as ``(value - offset) / scale``."""

    class Dev(Component):
        temp = gauge(0, 0.1, offset=-100.0, writable=True)

    unit = MockModbusConnection().for_unit(1)
    dev = Dev(unit)
    await dev.write("temp", 50.0)  # (50 - -100) / 0.1 = 1500
    assert unit.holding[0] == 1500
    await dev.async_update()
    assert dev.temp == pytest.approx(50.0)


async def test_scaled_float_round_trips_on_write() -> None:
    """A writable scaled float inverts its scale on write (no offset)."""

    class Dev(Component):
        value = float32(0, scale=0.1, writable=True)  # raw -> raw * 0.1

    unit = MockModbusConnection().for_unit(1)
    dev = Dev(unit)
    await dev.write("value", 5.0)  # 5.0 / 0.1 = 50.0 stored, not 5.0
    assert decode_float32([unit.holding[0], unit.holding[1]]) == pytest.approx(50.0)
    await dev.async_update()
    assert dev.value == pytest.approx(5.0)


async def test_float_offset_round_trips_on_write() -> None:
    """A writable float field inverts both scale and offset on write."""

    class Dev(Component):
        value = float32(0, scale=2.0, offset=1.0, writable=True)  # raw -> raw*2 + 1

    unit = MockModbusConnection().for_unit(1)
    dev = Dev(unit)
    await dev.write("value", 11.0)  # (11 - 1) / 2 = 5.0 stored
    await dev.async_update()
    assert dev.value == pytest.approx(11.0)
    assert decode_float32([unit.holding[0], unit.holding[1]]) == pytest.approx(5.0)


async def test_dynamic_scale_register_with_offset() -> None:
    """An offset adds on top of a dynamic ``10**sf`` scale factor."""

    class Scaled(Component):
        current = gauge(0, 1.0, offset=5.0, signed=False, scale_register=1)

    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 1234, 1: (-2) & 0xFFFF})  # 1234 * 10**-2 + 5
    scaled = Scaled(unit)
    await scaled.async_update()
    assert scaled.current == pytest.approx(17.34)


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
    bit_blocks = meter._bit_blocks
    await meter.async_update()
    await meter.async_update()
    # The cached_property plan is the same object each poll, never rebuilt.
    assert meter._register_blocks is register_blocks
    assert meter._bit_blocks is bit_blocks


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


def _calls_recording_unit() -> tuple[MockModbusUnit, list[tuple]]:
    """A mock unit that records each register-write call as ``(fc, *args)``."""
    unit = MockModbusConnection().for_unit(1)
    calls: list[tuple] = []
    real_single = unit.write_register
    real_multi = unit.write_registers

    async def write_register(address: int, value: int) -> None:
        calls.append(("single", address, value))
        await real_single(address, value)

    async def write_registers(address: int, values: list[int]) -> None:
        calls.append(("multiple", address, values))
        await real_multi(address, values)

    unit.write_register = write_register  # type: ignore[method-assign]
    unit.write_registers = write_registers  # type: ignore[method-assign]
    return unit, calls


async def test_single_register_uses_fc06_by_default() -> None:
    """A one-word write picks FC06 (write-single-register) by default."""

    class Dev(Component):
        setpoint = integer(0, signed=False, writable=True)

    unit, calls = _calls_recording_unit()
    await Dev(unit).write("setpoint", 1234)
    assert calls == [("single", 0, 1234)]
    assert unit.holding[0] == 1234


async def test_force_fc16_uses_multiple_for_single_register() -> None:
    """``force_fc16`` writes a one-register field with FC16 (solax/sunsynk)."""

    class Dev(Component):
        setpoint = integer(0, signed=False, writable=True, force_fc16=True)

    unit, calls = _calls_recording_unit()
    await Dev(unit).write("setpoint", 7)
    assert calls == [("multiple", 0, [7])]
    assert unit.holding[0] == 7


def test_force_fc16_requires_writable() -> None:
    """force_fc16 only affects writes, so it's a misconfig on a read-only field."""
    with pytest.raises(ValueError, match="force_fc16 requires writable"):
        integer(0, force_fc16=True)


def _bounded(low: int, high: int) -> Callable[[Any], int]:
    """A WriteValidator that rejects values outside ``[low, high]``."""

    def validate(value: int) -> int:
        if not low <= value <= high:
            raise ValueError(f"{value} out of range [{low}, {high}]")
        return value

    return validate


async def test_validator_makes_field_writable() -> None:
    class Dev(Component):
        setpoint = integer(0, writable=_bounded(0, 100))

    unit = MockModbusConnection().for_unit(1)
    dev = Dev(unit)
    await dev.write("setpoint", 42)  # in range -> written
    await dev.async_update()
    assert dev.setpoint == 42


async def test_validator_rejects_value_before_writing() -> None:
    class Dev(Component):
        setpoint = integer(0, writable=_bounded(0, 100))

    unit = MockModbusConnection().for_unit(1)
    dev = Dev(unit)
    with pytest.raises(ValueError, match="out of range"):
        await dev.write("setpoint", 250)
    assert 0 not in unit.holding  # nothing reached the device


async def test_validator_can_coerce_the_written_value() -> None:
    class Dev(Component):
        # Clamp into range instead of rejecting.
        setpoint = integer(0, writable=lambda v: max(0, min(100, v)))

    unit = MockModbusConnection().for_unit(1)
    dev = Dev(unit)
    await dev.write("setpoint", 250)
    await dev.async_update()
    assert dev.setpoint == 100  # the coerced value was written


async def test_coil_validator_rejects_value() -> None:
    locked = False

    def reject_when_locked(value: bool) -> bool:
        if locked:
            raise ValueError("relay is locked")
        return value

    class Dev(Component):
        relay = coil(0, writable=reject_when_locked)

    unit = MockModbusConnection().for_unit(1)
    dev = Dev(unit)
    await dev.write("relay", True)  # not locked -> written
    await dev.async_update()
    assert dev.relay is True

    locked = True
    with pytest.raises(ValueError, match="locked"):
        await dev.write("relay", False)


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

    async def read_coils(self, address: int, count: int) -> list[bool]:
        self.reads.append(("coil", address, count))
        return await self._inner.read_coils(address, count)

    async def read_discrete_inputs(self, address: int, count: int) -> list[bool]:
        self.reads.append(("discrete", address, count))
        return await self._inner.read_discrete_inputs(address, count)

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


# -- discrete inputs (FC02) ---------------------------------------------------


def test_coil_and_discrete_factories_return_their_field_types() -> None:
    assert isinstance(coil(0), CoilField)
    assert isinstance(discrete_input(0), DiscreteInputField)
    assert coil(0).space == "coil"
    assert discrete_input(0).space == "discrete"


async def test_discrete_input_reads_via_fc02() -> None:
    class Sensors(Component):
        alarm = discrete_input(1)

    inner = MockModbusConnection().for_unit(1)
    inner.discrete_inputs[1] = True
    inner.coils[1] = False  # would read False if (wrongly) read from coils
    unit = _SpyUnit(inner)
    sensors = Sensors(unit)  # type: ignore[arg-type]
    await sensors.async_update()
    assert sensors.alarm is True
    assert ("discrete", 1, 1) in unit.reads


async def test_component_mixes_coils_and_discrete_inputs() -> None:
    class Mixed(Component):
        relay = coil(0, writable=True)
        fault = discrete_input(0)  # same address number, different space

    inner = MockModbusConnection().for_unit(1)
    inner.coils[0] = True
    inner.discrete_inputs[0] = False
    unit = _SpyUnit(inner)
    mixed = Mixed(unit)  # type: ignore[arg-type]
    await mixed.async_update()
    assert mixed.relay is True
    assert mixed.fault is False
    # Same address number but distinct spaces: never merged into one read.
    assert ("coil", 0, 1) in unit.reads
    assert ("discrete", 0, 1) in unit.reads


async def test_write_to_discrete_input_raises() -> None:
    class Sensors(Component):
        alarm = discrete_input(0)

    unit = MockModbusConnection().for_unit(1)
    with pytest.raises(AttributeError, match="read-only"):
        await Sensors(unit).write("alarm", True)


async def test_discrete_input_modbus_exception_decodes_to_none() -> None:
    class Failing:
        async def read_discrete_inputs(self, address: int, count: int) -> list[bool]:
            raise ModbusExceptionError(2, "illegal data address")

        async def read_coils(self, address: int, count: int) -> list[bool]:
            raise AssertionError("no coils to read")  # no coil fields declared

    class Sensors(Component):
        alarm = discrete_input(0)

    sensors = Sensors(Failing())  # type: ignore[arg-type]
    await sensors.async_update()
    assert sensors.alarm is None


async def test_group_pools_discrete_inputs() -> None:
    class A(Component):
        a = discrete_input(0)

    class B(Component):
        b = discrete_input(1)

    inner = MockModbusConnection().for_unit(1)
    inner.discrete_inputs.update({0: True, 1: True})
    unit = _SpyUnit(inner)
    a, b = A(unit), B(unit)  # type: ignore[arg-type]
    await ComponentGroup(unit, [a, b]).async_update()  # type: ignore[list-item]
    assert a.a is True and b.b is True
    # Both discrete inputs fetched in one pooled read.
    assert ("discrete", 0, 2) in unit.reads


async def test_coil_and_discrete_ranges_are_independent() -> None:
    class IO(Component):
        coil_ranges = ((0, 40),)  # coils: one readable block, 5..9 mergeable
        discrete_ranges = ((0, 6), (9, 40))  # discrete: 7-8 unreadable
        relay_lo = coil(5)
        relay_hi = coil(9)
        sensor_lo = discrete_input(5)
        sensor_hi = discrete_input(9)

    inner = MockModbusConnection().for_unit(1)
    inner.coils.update({5: True, 9: True})
    inner.discrete_inputs.update({5: True, 9: True})
    unit = _SpyUnit(inner)
    io = IO(unit)  # type: ignore[arg-type]
    await io.async_update()
    read = {
        (space, start + i) for space, start, count in unit.reads for i in range(count)
    }
    # Coils 5 and 9 share one range, so the merged read covers 7-8 too.
    assert ("coil", 7) in read
    # Discrete 7-8 are unreadable, so the two discrete spans stay separate.
    assert ("discrete", 7) not in read and ("discrete", 8) not in read
    assert io.relay_lo and io.relay_hi and io.sensor_lo and io.sensor_hi


async def test_group_rejects_mismatched_discrete_ranges() -> None:
    class A(Component):
        discrete_ranges = ((0, 10),)
        a = discrete_input(0)

    class B(Component):
        discrete_ranges = ((0, 20),)
        b = discrete_input(1)

    unit = MockModbusConnection().for_unit(1)
    with pytest.raises(ValueError, match="discrete_ranges"):
        ComponentGroup(unit, [A(unit), B(unit)])


async def test_group_reads_coils_and_discrete_inputs_separately() -> None:
    class Relays(Component):
        relay = coil(0)

    class Sensors(Component):
        fault = discrete_input(0)

    inner = MockModbusConnection().for_unit(1)
    inner.coils[0] = True
    inner.discrete_inputs[0] = False
    unit = _SpyUnit(inner)
    relays, sensors = Relays(unit), Sensors(unit)  # type: ignore[arg-type]
    await ComponentGroup(unit, [relays, sensors]).async_update()  # type: ignore[list-item]
    assert relays.relay is True
    assert sensors.fault is False
    assert ("coil", 0, 1) in unit.reads
    assert ("discrete", 0, 1) in unit.reads


# -- base_offset (uniform per-instance address shift) -------------------------


async def test_base_offset_shifts_every_field_and_bit() -> None:
    class Block(Component):
        w = integer(10, signed=False)
        v = integer(11, signed=False)
        on = coil(5)

    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({30: 100, 31: 220})  # 10, 11 shifted by +20
    unit.coils[25] = True  # 5 shifted by +20
    block = Block(unit, base_offset=20)
    await block.async_update()
    assert block.w == 100
    assert block.v == 220
    assert block.on is True


async def test_base_offset_defaults_to_zero() -> None:
    class Block(Component):
        w = integer(10, signed=False)

    unit = MockModbusConnection().for_unit(1)
    unit.holding[10] = 5
    block = Block(unit)  # no base_offset -> addresses unchanged
    await block.async_update()
    assert block.w == 5


async def test_base_offset_composes_with_index_stride() -> None:
    class Block(Component):
        w = integer(0, signed=False, stride=5)

    unit = MockModbusConnection().for_unit(1)
    # index=3 -> stride*(3-1)=10; +base_offset 100 -> address 0+10+100=110
    unit.holding[110] = 7
    block = Block(unit, index=3, base_offset=100)
    await block.async_update()
    assert block.w == 7


async def test_base_offset_shifts_writes() -> None:
    class Block(Component):
        setpoint = integer(10, signed=False, writable=True)

    unit, calls = _calls_recording_unit()
    await Block(unit, base_offset=20).write("setpoint", 42)
    assert calls == [("single", 30, 42)]
    assert unit.holding[30] == 42


async def test_base_offset_does_not_shift_scale_register() -> None:
    # A SunSpec repeating block scales off a shared sunssf in the fixed block:
    # the value address shifts with base_offset, the scale register does not.
    class Block(Component):
        w = gauge(10, 1.0, scale_register=2)

    unit = MockModbusConnection().for_unit(1)
    unit.holding[2] = (-2) & 0xFFFF  # sf = -2, at its fixed (unshifted) address
    unit.holding[30] = 1234  # value at 10, shifted by +20
    block = Block(unit, base_offset=20)
    await block.async_update()
    assert block.w == pytest.approx(12.34)  # 1234 * 10**-2
