"""Tests for the device-modelling framework (modbus_connection.model)."""

from __future__ import annotations

import struct
from collections.abc import Callable
from enum import IntEnum, IntFlag
from typing import Any

import pytest

from modbus_connection.decode import decode_float32
from modbus_connection.exceptions import BlockReadError, ModbusExceptionError
from modbus_connection.mock import (
    MockModbusConnection,
    MockModbusUnit,
    ReadEvent,
    WriteEvent,
)
from modbus_connection.model import (
    Component,
    ComponentGroup,
    ManualComponent,
    ResolvedField,
    bit,
    bits,
    boolean,
    coil,
    discrete_input,
    enum,
    flags,
    float32,
    float64,
    gauge,
    int32,
    int64,
    integer,
    raw_register,
    repeating_group,
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
    RegisterField,
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


@pytest.mark.parametrize("raw", [0x8000, 0xF448])
async def test_several_nan_sentinels_all_decode_to_none(raw: int) -> None:
    """A device may define distinct 'no value' codes for the same register."""

    class Lambda(Component):
        # 0x8000: register not present. 0xF448 (-3000 signed): sensor unplugged.
        temperature = gauge(0, 0.1, nan=(0x8000, 0xF448), unit="°C")

    unit = MockModbusConnection().for_unit(1)
    unit.holding[0] = raw
    dev = Lambda(unit)
    await dev.async_update()
    assert dev.temperature is None


async def test_several_nan_sentinels_leave_real_values_alone() -> None:
    class Lambda(Component):
        temperature = gauge(0, 0.1, nan=(0x8000, 0xF448), unit="°C")

    unit = MockModbusConnection().for_unit(1)
    unit.holding[0] = 225
    dev = Lambda(unit)
    await dev.async_update()
    assert dev.temperature == pytest.approx(22.5)


async def test_nan_sentinels_apply_to_integer_fields() -> None:
    class Dev(Component):
        code = integer(0, signed=False, nan=(0xFFFF, 0x8000))

    unit = MockModbusConnection().for_unit(1)
    unit.holding[0] = 0xFFFF
    dev = Dev(unit)
    await dev.async_update()
    assert dev.code is None


async def test_an_empty_nan_iterable_means_no_sentinel() -> None:
    class Dev(Component):
        value = integer(0, signed=False, nan=())

    unit = MockModbusConnection().for_unit(1)
    unit.holding[0] = 0x8000
    dev = Dev(unit)
    await dev.async_update()
    assert dev.value == 0x8000


async def test_nan_sentinels_apply_to_multi_register_fields() -> None:
    """A 32-bit register has a sentinel too, and it spans both words."""

    class Dev(Component):
        energy = uint32(0, scale=0.1, nan=0xFFFFFFFF, unit="kWh")
        power = int32(2, nan=0x80000000, unit="W")

    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 0xFFFF, 1: 0xFFFF, 2: 0x8000, 3: 0x0000})
    dev = Dev(unit)
    await dev.async_update()
    assert dev.energy is None
    assert dev.power is None


async def test_a_signed_sentinel_leaves_a_real_negative_alone() -> None:
    """0xFFFFFFFF is -1 W to a signed field, not 'no value'."""

    class Dev(Component):
        power = int32(0, nan=0x80000000, unit="W")

    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 0xFFFF, 1: 0xFFFF})
    dev = Dev(unit)
    await dev.async_update()
    assert dev.power == -1


async def test_nan_sentinels_apply_to_float_fields() -> None:
    """Some devices mark a float unimplemented with all ones rather than a NaN."""

    class Dev(Component):
        pressure = float32(0, nan=0xFFFFFFFF, unit="bar")

    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 0xFFFF, 1: 0xFFFF})
    dev = Dev(unit)
    await dev.async_update()
    assert dev.pressure is None


async def test_nan_sentinels_apply_to_64_bit_fields() -> None:
    class Dev(Component):
        counter = uint64(0, nan=0xFFFFFFFFFFFFFFFF)
        offset = int64(4, nan=0x8000000000000000)
        reading = float64(8, nan=0xFFFFFFFFFFFFFFFF)

    unit = MockModbusConnection().for_unit(1)
    unit.holding.update(dict.fromkeys(range(0, 4), 0xFFFF))
    unit.holding.update({4: 0x8000, 5: 0, 6: 0, 7: 0})
    unit.holding.update(dict.fromkeys(range(8, 12), 0xFFFF))
    dev = Dev(unit)
    await dev.async_update()
    assert dev.counter is None
    assert dev.offset is None
    assert dev.reading is None


async def test_a_sentinel_is_the_assembled_value_not_the_wire_order() -> None:
    """The sentinel is compared after the words are combined, so it reads the
    same whichever way round the device sends them."""

    class Dev(Component):
        energy = uint32(0, word_order="little", nan=0xDEADBEEF)

    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 0xBEEF, 1: 0xDEAD})  # low word first
    dev = Dev(unit)
    await dev.async_update()
    assert dev.energy is None


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


async def test_plan_is_built_once_across_polls() -> None:
    meter = _meter({0: 7})
    await meter.async_update()
    plan = meter._plan
    await meter.async_update()
    await meter.async_update()
    # The cached plan is the same object each poll, never rebuilt.
    assert meter._plan is plan


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
    class Mode(IntEnum):
        OFF = 0

    class Dev(Component):
        mode = enum(0, Mode)

    unit = MockModbusConnection().for_unit(1)
    unit.holding[0] = 9  # not a Mode member
    dev = Dev(unit)
    await dev.async_update()
    assert dev.mode is None


async def test_convert_function(caplog: pytest.LogCaptureFixture) -> None:
    """Any callable works as ``convert``; a ValueError decodes to None, warned once."""
    import logging

    def parity(raw: int) -> str:
        if raw > 2:
            raise ValueError(raw)
        return "even" if raw % 2 == 0 else "odd"

    class Dev(Component):
        first: NumberField[str] = NumberField(0, convert=parity)
        second: NumberField[str] = NumberField(1, convert=parity)

    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 2, 1: 9})  # 9 is rejected by the converter
    dev = Dev(unit)
    with caplog.at_level(logging.WARNING, logger="modbus_connection.model"):
        await dev.async_update()
        assert dev.first == "even"
        assert dev.second is None
        await dev.async_update()  # second poll: no second warning
    warnings = [r for r in caplog.records if "no mapping for value 9" in r.message]
    assert len(warnings) == 1
    assert "parity" in warnings[0].message


async def test_convert_mapping(caplog: pytest.LogCaptureFixture) -> None:
    """A dict works as ``convert``; a missing key decodes to None, warned once."""
    import logging

    class Dev(Component):
        state: NumberField[str] = NumberField(0, convert={1: "on", 2: "off"})
        unknown: NumberField[str] = NumberField(1, convert={1: "on", 2: "off"})

    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 1, 1: 9})  # 9 has no mapping
    dev = Dev(unit)
    with caplog.at_level(logging.WARNING, logger="modbus_connection.model"):
        await dev.async_update()
        assert dev.state == "on"
        assert dev.unknown is None
        await dev.async_update()  # second poll: no second warning
    warnings = [r for r in caplog.records if "no mapping for value 9" in r.message]
    assert len(warnings) == 1


async def test_convert_callable_keyerror_propagates() -> None:
    """Only ValueError means "unknown" from a callable; KeyError is a bug."""

    class Dev(Component):
        broken: NumberField[str] = NumberField(0, convert={1: "on"}.__getitem__)

    unit = MockModbusConnection().for_unit(1)
    unit.holding[0] = 9
    dev = Dev(unit)
    with pytest.raises(Exception) as excinfo:
        await dev.async_update()
    assert isinstance(excinfo.value, KeyError) or isinstance(
        excinfo.value.__cause__, KeyError
    )


async def test_enum_type_is_an_alias_for_convert() -> None:
    """The pre-``convert`` kwarg keeps working; passing both is rejected."""

    class Mode(IntEnum):
        OFF = 0
        ON = 1

    class Dev(Component):
        mode: NumberField[Mode] = NumberField(0, signed=False, enum_type=Mode)

    unit = MockModbusConnection().for_unit(1)
    unit.holding[0] = 1
    dev = Dev(unit)
    await dev.async_update()
    assert dev.mode is Mode.ON

    with pytest.raises(ValueError, match="either convert or enum_type"):
        NumberField(0, convert=Mode, enum_type=Mode)


class _Relay(Component):
    """On/off state held in holding registers, as devices without coils do."""

    output = boolean(0, writable=True)
    alarm = boolean(1)
    fan = boolean(2, nan=0xFFFF)


async def test_boolean_decodes_codes_and_rejects_out_of_spec() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 1, 1: 0, 2: 7})  # 7 is neither 0 nor 1
    dev = _Relay(unit)
    await dev.async_update()
    assert dev.output is True
    assert dev.alarm is False
    assert dev.fan is None


async def test_boolean_nan_sentinel() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 1, 1: 1, 2: 0xFFFF})
    dev = _Relay(unit)
    await dev.async_update()
    assert dev.fan is None


async def test_boolean_write_round_trips() -> None:
    unit = MockModbusConnection().for_unit(1)
    dev = _Relay(unit)
    await dev.write("output", True)
    assert (await unit.read_holding_registers(0, 1))[0] == 1
    await dev.write("output", False)
    assert (await unit.read_holding_registers(0, 1))[0] == 0


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


# -- packed bit fields --------------------------------------------------------


class SiteLimit(Component):
    """Five independent settings packed into one register."""

    limit_mode = bits(0, 0, 3, writable=True)
    external_production = bit(0, 10, writable=True)
    negative_limit = bit(0, 11, writable=True)
    reserved = bits(0, 12, 4)


def _site_limit(word: int) -> SiteLimit:
    unit = MockModbusConnection().for_unit(1)
    unit.holding[0] = word
    return SiteLimit(unit)


async def test_packed_bits_decode_from_one_read() -> None:
    component = _site_limit(0b0011_1100_0000_0010)
    await component.async_update()
    assert component.limit_mode == 2
    assert component.external_production is True
    assert component.negative_limit is True
    assert component.reserved == 0b0011
    # Four fields over one register cost one block read.
    assert component._unit.read_events == [
        ReadEvent(register_type="holding", address=0, count=1)
    ]


async def test_packed_write_keeps_a_bit_changed_since_the_last_poll() -> None:
    """The write re-reads, so a concurrent change survives it."""
    component = _site_limit(0b0000_0000_0000_0010)
    await component.async_update()
    assert component.negative_limit is False

    # Something else sets bit 11 after that poll: the installer app, the
    # device itself, another entity in the same tick.
    component._unit.holding[0] = 0b0000_1000_0000_0010

    await component.write("external_production", True)
    assert component._unit.holding[0] == 0b0000_1100_0000_0010


async def test_packed_write_sets_a_bit_run() -> None:
    component = _site_limit(0b0000_1000_0000_0111)
    await component.write("limit_mode", 2)
    assert component._unit.holding[0] == 0b0000_1000_0000_0010


async def test_packed_write_clears_a_bit() -> None:
    component = _site_limit(0b0000_1100_0000_0010)
    await component.write("external_production", False)
    assert component._unit.holding[0] == 0b0000_1000_0000_0010


async def test_packed_write_rejects_a_value_too_wide() -> None:
    component = _site_limit(0)
    with pytest.raises(ValueError, match="does not fit the 3 bit"):
        await component.write("limit_mode", 8)
    assert component._unit.holding[0] == 0  # nothing written


async def test_packed_write_rejects_a_read_only_field() -> None:
    component = _site_limit(0)
    with pytest.raises(AttributeError):
        await component.write("reserved", 1)


async def test_packed_write_runs_its_validator() -> None:
    def only_two(value: int) -> int:
        if value != 2:
            raise ValueError("only mode 2")
        return value

    class Guarded(Component):
        mode = bits(0, 0, 3, writable=only_two)

    unit = MockModbusConnection().for_unit(1)
    unit.holding[0] = 0
    component = Guarded(unit)
    with pytest.raises(ValueError, match="only mode 2"):
        await component.write("mode", 1)
    await component.write("mode", 2)
    assert unit.holding[0] == 2


def test_packed_bits_must_fit_a_register() -> None:
    with pytest.raises(ValueError, match="do not fit"):
        bits(0, 14, 4)
    with pytest.raises(ValueError, match="do not fit"):
        bit(0, 16)


# -- resolved fields ----------------------------------------------------------


class _Point(Component):
    v = integer(0, scale_register=1, writable=True)
    v_sf = integer(1)


class _Placed(Component):
    energy = uint32(10)
    relay = coil(3)
    pt = repeating_group(2, _Point, stride=2)


def test_resolved_fields_resolve_addresses() -> None:
    unit = MockModbusConnection().for_unit(1)
    component = _Placed(unit, base_offset=40000)
    resolved = component.resolved_fields
    assert resolved["energy"] == ResolvedField(
        component.declared_fields["energy"], 40010, 2, None, "holding"
    )
    assert resolved["relay"] == ResolvedField(
        component.declared_fields["relay"], 40003, 1, None, "coil"
    )
    assert list(resolved) == ["energy", "relay"]  # declaration order


def test_resolved_fields_of_a_sub_instance() -> None:
    """The instance shift is in the address; a shared scale factor is not."""
    unit = MockModbusConnection().for_unit(1)
    component = _Placed(unit, base_offset=40000)
    resolved = component.pt[1].resolved_fields["v"]
    assert (resolved.address, resolved.scale_address) == (40002, 40001)


def test_resolved_fields_of_a_sub_instance_with_scale_in_block() -> None:
    class _OwnScale(_Point):
        scale_in_block = True

    class _Owner(Component):
        pt = repeating_group(2, _OwnScale, stride=2)

    unit = MockModbusConnection().for_unit(1)
    resolved = _Owner(unit, base_offset=100).pt[1].resolved_fields["v"]
    assert (resolved.address, resolved.scale_address) == (102, 103)


def test_resolved_fields_follow_index_and_stride() -> None:
    class _Channel(Component):
        temperature = gauge(12, 0.1, stride=4)

    unit = MockModbusConnection().for_unit(1)
    assert _Channel(unit, index=3).resolved_fields["temperature"].address == 20


async def test_write_rejects_an_unknown_field() -> None:
    unit = MockModbusConnection().for_unit(1)
    with pytest.raises(AttributeError, match="unknown field"):
        await _Placed(unit).write("nope", 1)


def test_resolved_fields_narrow_with_restrict_fields() -> None:
    class Meter(Component):
        voltage = gauge(0, 0.1)
        current = gauge(1, 0.1)

    unit = MockModbusConnection().for_unit(1)
    component = Meter(unit)
    component.restrict_fields(["current"])
    assert list(component.resolved_fields) == ["current"]
    assert "voltage" in component.declared_fields  # the declared layout is intact


def test_modbus_unit_is_public() -> None:
    unit = MockModbusConnection().for_unit(1)
    component = _Placed(unit, base_offset=40000)
    assert component.modbus_unit is unit
    # including on an instance the caller never built
    assert component.pt[0].modbus_unit is unit


async def test_a_field_may_be_named_unit() -> None:
    """The accessor does not take a name a device library wants."""

    class Sensor(Component):
        value = integer(0)
        unit = integer(1)  # a unit-of-measure code

    modbus_unit = MockModbusConnection().for_unit(1)
    modbus_unit.holding.update({0: 42, 1: 7})
    sensor = Sensor(modbus_unit)
    await sensor.async_update()
    assert (sensor.value, sensor.unit) == (42, 7)
    assert sensor.modbus_unit is modbus_unit


async def test_resolved_fields_support_batching_a_write() -> None:
    """Two adjacent fields, encoded and written in one request."""
    unit = MockModbusConnection().for_unit(1)
    unit.holding[1] = 0  # the shared scale factor
    component = _Placed(unit, base_offset=40000)
    points = [c.resolved_fields["v"] for c in component.pt]
    assert [p.address for p in points] == [40000, 40002]  # stride 2, one word each

    words: list[int] = []
    for resolved, value in zip(points, (11, 22), strict=True):
        words.extend(resolved.field.encode(value, 0))
        words.extend([0])  # v_sf, untouched between the two points
    await component.modbus_unit.write_registers(points[0].address, words[:3])
    assert await unit.read_holding_registers(40000, 3) == [11, 0, 22]


# -- dynamically-scaled writes -------------------------------------------------


class _Scaled(Component):
    setpoint = gauge(10, 1.0, scale_register=2, writable=True)


async def test_write_scaled_field() -> None:
    # no prior update needed: the scale factor is read as part of the write
    unit = MockModbusConnection().for_unit(1)
    unit.holding[2] = (-1) & 0xFFFF  # sf = -1
    await _Scaled(unit).write("setpoint", 50.0)
    assert unit.holding[10] == 500


async def test_write_scaled_field_reads_scale_factor_fresh() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding[2] = (-1) & 0xFFFF
    dev = _Scaled(unit)
    await dev.async_update()
    # the device shifts the scale factor after the poll; the write must
    # encode with the current factor, not the polled one
    unit.holding[2] = (-2) & 0xFFFF
    await dev.write("setpoint", 5.0)
    assert unit.holding[10] == 500


async def test_write_scaled_field_positive_exponent_and_signed() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding[2] = 2  # sf = 2: raw is in hundreds
    dev = _Scaled(unit)
    await dev.write("setpoint", 1200)
    assert unit.holding[10] == 12
    await dev.write("setpoint", -1200)
    assert unit.holding[10] == 0x10000 - 12


async def test_write_scaled_field_rounds_to_step() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding[2] = (-2) & 0xFFFF  # steps of 0.01
    await _Scaled(unit).write("setpoint", 12.349)
    assert unit.holding[10] == 1235


async def test_write_scaled_field_not_implemented_factor() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding[2] = 0x8000  # the SunSpec sunssf "not implemented" sentinel
    with pytest.raises(ValueError, match="unusable scale factor -32768"):
        await _Scaled(unit).write("setpoint", 50.0)
    assert 10 not in unit.holding  # nothing was written


async def test_write_scaled_field_overflowing_factor() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding[2] = 32000  # 10**32000 is not representable
    with pytest.raises(ValueError, match="unusable scale factor 32000"):
        await _Scaled(unit).write("setpoint", 50.0)
    assert 10 not in unit.holding


async def test_write_scaled_field_with_base_offset() -> None:
    # the scale factor is read at the placed block's address
    unit = MockModbusConnection().for_unit(1)
    unit.holding[102] = (-1) & 0xFFFF
    await _Scaled(unit, base_offset=100).write("setpoint", 50.0)
    assert unit.holding[110] == 500


async def test_write_scaled_float_field() -> None:
    class Dev(Component):
        setpoint = FloatField(10, count=2, scale_register=2, writable=True)

    unit = MockModbusConnection().for_unit(1)
    unit.holding[2] = (-1) & 0xFFFF
    dev = Dev(unit)
    await dev.write("setpoint", 12.5)  # stored as 125.0
    await dev.async_update()
    assert dev.setpoint == 12.5


async def test_manual_component_scaled_write() -> None:
    unit = MockModbusConnection().for_unit(1)
    manual = ManualComponent(unit)
    manual.add("setpoint", gauge(10, 1.0, scale_register=2, writable=True))
    unit.holding[2] = (-1) & 0xFFFF
    await manual.write("setpoint", 50.0)
    assert unit.holding[10] == 500


def _write_recording_unit() -> tuple[MockModbusUnit, list[WriteEvent]]:
    """A mock unit whose writes are captured as ``WriteEvent``s."""
    unit = MockModbusConnection().for_unit(1)
    events: list[WriteEvent] = []
    unit.on_write(events.append)
    return unit, events


async def test_single_register_uses_fc06_by_default() -> None:
    """A one-word write picks FC06 (write-single-register) by default."""

    class Dev(Component):
        setpoint = integer(0, signed=False, writable=True)

    unit, events = _write_recording_unit()
    await Dev(unit).write("setpoint", 1234)
    assert events == [WriteEvent("holding", 0, [1234], 0x06)]
    assert unit.holding[0] == 1234


async def test_force_fc16_uses_multiple_for_single_register() -> None:
    """``force_fc16`` writes a one-register field with FC16 (solax/sunsynk)."""

    class Dev(Component):
        setpoint = integer(0, signed=False, writable=True, force_fc16=True)

    unit, events = _write_recording_unit()
    await Dev(unit).write("setpoint", 7)
    assert events == [WriteEvent("holding", 0, [7], 0x10)]
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


async def test_update_without_notifying() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 7})
    a = Meter(unit)
    calls: list[int] = []
    a.add_update_listener(lambda: calls.append(1))

    await a.async_update(notify=False)
    assert a.count == 7  # the values refresh
    assert calls == []  # but no listener fires

    a.notify()  # the caller notifies itself
    assert calls == [1]


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


def test_plan_blocks_rejects_span_running_past_a_range_end() -> None:
    # A 2-register field at 5 covers 5..6, but the device answers 0-5: the layout
    # contradicts itself, so it is rejected instead of planned as if it fit.
    with pytest.raises(ValueError, match="does not fit inside a readable range"):
        plan_blocks([(0, 1), (5, 2)], ((0, 5),))


def test_plan_blocks_rejects_span_starting_before_a_range() -> None:
    # The mirror image: the field ends inside a range but starts below its low.
    with pytest.raises(ValueError, match="does not fit inside a readable range"):
        plan_blocks([(4, 2)], ((5, 10),))


def test_plan_blocks_rejects_span_bridging_two_ranges() -> None:
    with pytest.raises(ValueError, match="does not fit inside a readable range"):
        plan_blocks([(6, 4)], ((0, 6), (9, 40)))  # 6..9 spans the 7-8 gap


def test_plan_blocks_rejects_span_outside_every_range() -> None:
    # A field wholly at addresses the map says the device never answers is as
    # unreadable as one crossing a boundary.
    with pytest.raises(ValueError, match="does not fit inside a readable range"):
        plan_blocks([(0, 1), (20, 2)], ((0, 5),))


def test_plan_blocks_allows_span_ending_on_a_range_end() -> None:
    # Fitting exactly is fine; only running past the high is an error.
    assert plan_blocks([(0, 1), (4, 2)], ((0, 5),)) == [(0, 6)]


def test_plan_blocks_rejects_overlapping_ranges() -> None:
    with pytest.raises(ValueError, match="overlap"):
        plan_blocks([(5, 1)], ((0, 40), (30, 60)))  # 30-40 in both ranges


def test_plan_blocks_rejects_reversed_range() -> None:
    with pytest.raises(ValueError, match="reversed"):
        plan_blocks([(5, 1)], ((40, 0),))


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
    assert comp._plan is not None and comp._plan.blocks["holding"] == [(0, 11)]
    assert comp.a == 1 and comp.b == 2


async def test_component_rejects_field_past_its_readable_range() -> None:
    # A 32-bit field starting at the last readable address ends one past it; the
    # component looks constrained but cannot be read, so planning says so.
    class Wide(Component):
        register_ranges = ((0, 5),)  # the device answers 0-5 and nothing above
        first = integer(0)
        energy = uint32(5)  # spans 5..6

    unit = MockModbusConnection().for_unit(1)
    with pytest.raises(ValueError, match="does not fit inside a readable range"):
        await Wide(unit).async_update()


async def test_component_rejects_field_outside_its_readable_ranges() -> None:
    class Wide(Component):
        register_ranges = ((0, 5),)  # the device answers 0-5 and nothing above
        first = integer(0)
        stray = integer(20)  # entirely outside the map

    unit = MockModbusConnection().for_unit(1)
    with pytest.raises(ValueError, match="does not fit inside a readable range"):
        await Wide(unit).async_update()


async def test_group_members_may_differ_in_max_gap() -> None:
    """Each member's own max_gap shapes its own blocks, so they need not agree."""

    class Wide(Component):
        max_gap = 16
        a = integer(0)
        b = integer(10)  # bridged

    class Narrow(Component):
        max_gap = 0
        a = integer(100)
        b = integer(110)  # not bridged

    inner = MockModbusConnection().for_unit(1)
    unit = _SpyUnit(inner)
    group = ComponentGroup(unit, [Wide(unit), Narrow(unit)])  # type: ignore[list-item]
    await group.async_update()
    assert sorted(unit.reads) == [
        ("holding", 0, 11),
        ("holding", 100, 1),
        ("holding", 110, 1),
    ]


async def test_group_rejects_mismatched_max_span() -> None:
    class A(Component):
        max_span = 40
        x = integer(0)

    class B(Component):
        max_span = 125
        y = integer(0)

    unit = MockModbusConnection().for_unit(1)
    with pytest.raises(ValueError, match="max_span"):
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


async def test_discrete_input_modbus_exception_raises() -> None:
    class Failing:
        async def read_discrete_inputs(self, address: int, count: int) -> list[bool]:
            raise ModbusExceptionError(2, "illegal data address")

        async def read_coils(self, address: int, count: int) -> list[bool]:
            raise AssertionError("no coils to read")  # no coil fields declared

    class Sensors(Component):
        alarm = discrete_input(0)

    sensors = Sensors(Failing())  # type: ignore[arg-type]
    with pytest.raises(BlockReadError) as exc_info:
        await sensors.async_update()
    assert exc_info.value.space == "discrete"
    assert exc_info.value.exception_code == 2
    assert isinstance(exc_info.value.__cause__, ModbusExceptionError)


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

    unit, events = _write_recording_unit()
    await Block(unit, base_offset=20).write("setpoint", 42)
    assert events == [WriteEvent("holding", 30, [42], 0x06)]
    assert unit.holding[30] == 42


async def test_base_offset_shifts_scale_register() -> None:
    # base_offset places the whole layout — the scale register belongs to the
    # block and moves with it (a SunSpec model at its discovered address).
    class Block(Component):
        w = gauge(10, 1.0, scale_register=2)

    unit = MockModbusConnection().for_unit(1)
    unit.holding[22] = (-2) & 0xFFFF  # sf = -2, at 2 shifted by +20
    unit.holding[30] = 1234  # value at 10, shifted by +20
    block = Block(unit, base_offset=20)
    await block.async_update()
    assert block.w == pytest.approx(12.34)  # 1234 * 10**-2


async def test_base_offset_composes_with_scale_register_stride() -> None:
    # index/stride repeat within the placed block: the value follows stride,
    # the per-instance scale register follows scale_register_stride, and both
    # shift with base_offset.
    class Block(Component):
        w = gauge(10, 1.0, stride=5, scale_register=2, scale_register_stride=1)

    unit = MockModbusConnection().for_unit(1)
    # index=2: value at 10+5 +100, sf at 2+1 +100
    unit.holding[103] = (-1) & 0xFFFF
    unit.holding[115] = 500
    block = Block(unit, index=2, base_offset=100)
    await block.async_update()
    assert block.w == pytest.approx(50.0)  # 500 * 10**-1


class _RangedBlock(Component):
    """A layout declared relative to its block start, gap included."""

    register_ranges = ((0, 5), (50, 50))  # 6-49 unreadable
    error_number = integer(0)
    state = integer(1)
    actual = gauge(2, 0.1)
    target = gauge(50, 0.1)


async def test_base_offset_shifts_register_ranges() -> None:
    # The ranges are part of the placed layout, so they move with it and the
    # pooled reads survive: without the shift they match nothing and the plan
    # collapses to one read per field.
    inner = MockModbusConnection().for_unit(1)
    inner.holding.update({2000: 0, 2001: 1, 2002: 480, 2050: 520})
    unit = _SpyUnit(inner)
    block = _RangedBlock(unit, base_offset=2000)  # type: ignore[arg-type]
    await block.async_update()
    assert unit.reads == [("holding", 2000, 3), ("holding", 2050, 1)]
    assert block.actual == pytest.approx(48.0)
    assert block.target == pytest.approx(52.0)


async def test_base_offset_shifts_coil_and_discrete_ranges() -> None:
    class Block(Component):
        coil_ranges = ((0, 6), (9, 40))  # 7-8 unreadable
        discrete_ranges = ((0, 6), (9, 40))
        near = coil(5)
        far = coil(9)
        flag = discrete_input(5)

    inner = MockModbusConnection().for_unit(1)
    inner.coils.update({105: True, 109: True})
    inner.discrete_inputs[105] = True
    unit = _SpyUnit(inner)
    block = Block(unit, base_offset=100)  # type: ignore[arg-type]
    await block.async_update()
    read = {
        (space, start + i) for space, start, count in unit.reads for i in range(count)
    }
    # The gap moved with the block: 107-108 unread, 7-8 no longer protected.
    assert ("coil", 107) not in read and ("coil", 108) not in read
    assert block.near and block.far and block.flag


async def test_group_merges_ranges_of_blocks_at_different_offsets() -> None:
    # Same layout placed twice: each block contributes its own part of the
    # device's map, and the pooled plan keeps them apart.
    inner = MockModbusConnection().for_unit(1)
    inner.holding.update({2000: 0, 2002: 480, 2050: 520, 3000: 7, 3002: 490})
    unit = _SpyUnit(inner)
    group = ComponentGroup(  # type: ignore[list-item]
        unit,
        [_RangedBlock(unit, base_offset=2000), _RangedBlock(unit, base_offset=3000)],
    )
    await group.async_update()
    assert sorted(unit.reads) == [
        ("holding", 2000, 3),
        ("holding", 2050, 1),
        ("holding", 3000, 3),
        ("holding", 3050, 1),
    ]


async def test_group_rejects_ranges_that_overlap_without_agreeing() -> None:
    # Blocks close enough for their maps to overlap describe the same addresses
    # two ways, which is the disagreement the group guards against.
    unit = MockModbusConnection().for_unit(1)
    with pytest.raises(ValueError, match="register_ranges"):
        ComponentGroup(unit, [_RangedBlock(unit), _RangedBlock(unit, base_offset=3)])


class _SplitRun(Component):
    """One readable run declared as two touching spans, as a generator emits it."""

    register_ranges = ((0, 9), (10, 19))
    total = integer(0)


async def test_group_accepts_one_run_declared_as_touching_spans() -> None:
    # Same addresses, two shapes: one member's map is joined up, the other's is not.
    class Cell(Component):
        value = integer(0)  # no map of its own: the parent's covers it

    class WithGroup(_SplitRun):
        cells = repeating_group(2, Cell, stride=2)

    inner = MockModbusConnection().for_unit(1)
    inner.holding.update({0: 7, 2: 8})
    unit = _SpyUnit(inner)
    group = ComponentGroup(unit, [WithGroup(unit), _SplitRun(unit)])  # type: ignore[list-item]
    await group.async_update()
    # They agree, and the split both of them describe survives the merge.
    assert group._ranges.for_space("holding") == ((0, 9), (10, 19))


async def test_narrowed_components_with_interleaved_registers_pool() -> None:
    """A synthesised map is a claim: overlap with a sibling's is agreement.

    Devices interleave the registers of what a library models as separate
    components; narrowing each synthesises overlapping maps. Neither is a
    declaration of what the device serves, so the group must not read them
    as contradicting.
    """

    class Settings(Component):
        a = integer(100)
        b = integer(108)  # the synthesised map bridges 101..107
        c = integer(120)

    class Config(Component):
        x = integer(105)  # inside the run Settings' claim covers
        y = integer(300)

    inner = MockModbusConnection().for_unit(1)
    inner.holding.update({100: 1, 108: 2, 105: 5})
    unit = _SpyUnit(inner)
    settings, config = Settings(unit), Config(unit)  # type: ignore[arg-type]
    settings.restrict_fields(["a", "b"])
    config.restrict_fields(["x"])
    group = ComponentGroup(unit, [settings, config])  # type: ignore[list-item]
    await group.async_update()
    assert (settings.a, settings.b, config.x) == (1, 2, 5)
    assert settings.c is None and config.y is None


async def test_touching_claims_read_as_one_run() -> None:
    """Claims draw no boundary: adjacent narrowed components share a read."""

    class Left(Component):
        value = integer(100)
        dropped = integer(90)

    class Right(Component):
        value = integer(101)
        dropped = integer(110)

    inner = MockModbusConnection().for_unit(1)
    inner.holding.update({100: 1, 101: 2})
    unit = _SpyUnit(inner)
    left, right = Left(unit), Right(unit)  # type: ignore[arg-type]
    left.restrict_fields(["value"])
    right.restrict_fields(["value"])
    group = ComponentGroup(unit, [left, right])  # type: ignore[list-item]
    await group.async_update()
    assert (left.value, right.value) == (1, 2)
    assert unit.reads == [("holding", 100, 2)]


async def test_narrowed_member_agrees_with_a_declared_sibling() -> None:
    """A claim inside a sibling's declared run is agreement, not overlap."""

    class Narrowed(Component):
        value = integer(9)
        dropped = integer(12)

    class Declaring(Component):
        register_ranges = ((9, 30),)
        other = integer(30)

    inner = MockModbusConnection().for_unit(1)
    inner.holding.update({9: 7, 30: 8})
    unit = _SpyUnit(inner)
    narrowed = Narrowed(unit)
    narrowed.restrict_fields(["value"])
    group = ComponentGroup(unit, [narrowed, Declaring(unit)])  # type: ignore[list-item]
    await group.async_update()
    assert narrowed.value == 7 and narrowed.dropped is None


async def test_narrowing_a_declared_map_keeps_it_a_declaration() -> None:
    """Narrowing never turns a declared map into a claim: conflicts still raise."""

    class Narrowed(Component):
        register_ranges = ((0, 9),)
        value = integer(0)
        dropped = integer(5)

    class Other(Component):
        register_ranges = ((5, 14),)  # genuinely disagrees about 5..9
        other = integer(10)

    unit = MockModbusConnection().for_unit(1)
    narrowed = Narrowed(unit)
    narrowed.restrict_fields(["value"])
    with pytest.raises(ValueError, match="must agree on register_ranges"):
        ComponentGroup(unit, [narrowed, Other(unit)])


async def test_group_still_rejects_partly_overlapping_ranges() -> None:
    class Other(Component):
        register_ranges = ((5, 14), (15, 29))  # overlaps _SplitRun's run
        value = integer(5)

    unit = MockModbusConnection().for_unit(1)
    with pytest.raises(ValueError, match="must agree on register_ranges"):
        ComponentGroup(unit, [_SplitRun(unit), Other(unit)])


async def test_group_does_not_bridge_touching_ranges_of_two_members() -> None:
    class Low(Component):
        register_ranges = ((0, 9),)
        value = integer(9)

    class High(Component):
        register_ranges = ((10, 19),)
        value = integer(10)

    inner = MockModbusConnection().for_unit(1)
    unit = _SpyUnit(inner)
    group = ComponentGroup(unit, [Low(unit), High(unit)])  # type: ignore[list-item]
    assert group._ranges.for_space("holding") == ((0, 9), (10, 19))
    await group.async_update()
    assert sorted(unit.reads) == [("holding", 9, 1), ("holding", 10, 1)]


async def test_pooling_keeps_a_members_own_adjacent_boundaries() -> None:
    """A device serving adjacent banks refuses a read that crosses one."""

    class Banked(Component):
        register_ranges = ((0, 59), (60, 119), (120, 179))
        first = integer(58)
        second = integer(61)

    class Elsewhere(Component):
        other = integer(500)

    inner = MockModbusConnection().for_unit(1)
    unit = _SpyUnit(inner)
    group = ComponentGroup(unit, [Banked(unit), Elsewhere(unit)])  # type: ignore[list-item]
    assert group._ranges.for_space("holding") == (
        (0, 59),
        (60, 119),
        (120, 179),
        (500, 500),  # the undeclared member stands for what it reads
    )
    await group.async_update()
    assert ("holding", 58, 4) not in unit.reads
    assert sorted(unit.reads) == [
        ("holding", 58, 1),
        ("holding", 61, 1),
        ("holding", 500, 1),
    ]


async def test_group_does_not_bridge_a_gap_no_member_claims() -> None:
    class Low(Component):
        register_ranges = ((0, 9),)
        value = integer(9)

    class Far(Component):
        register_ranges = ((20, 29),)
        value = integer(20)

    inner = MockModbusConnection().for_unit(1)
    unit = _SpyUnit(inner)
    group = ComponentGroup(unit, [Low(unit), Far(unit)])  # type: ignore[list-item]
    assert group._ranges.for_space("holding") == ((0, 9), (20, 29))
    await group.async_update()
    assert sorted(unit.reads) == [("holding", 9, 1), ("holding", 20, 1)]


async def test_group_pools_a_member_that_declares_no_ranges() -> None:
    """A member with no map stands for what it reads, rather than raising."""

    class Unconstrained(Component):
        value = integer(100)
        other = integer(102)

    inner = MockModbusConnection().for_unit(1)
    unit = _SpyUnit(inner)
    group = ComponentGroup(  # type: ignore[list-item]
        unit, [_RangedBlock(unit), Unconstrained(unit)]
    )
    await group.async_update()
    assert sorted(unit.reads) == [
        ("holding", 0, 3),  # the ranged member, per its own map
        ("holding", 50, 1),
        ("holding", 100, 3),  # the unconstrained member, gap-planned as usual
    ]


async def test_group_does_not_bridge_between_members() -> None:
    """A gap no member claims is not read, even when max_gap would allow it."""

    class Low(Component):
        value = integer(0)

    class High(Component):
        value = integer(10)  # 10 - 0 <= the default max_gap of 16

    inner = MockModbusConnection().for_unit(1)
    unit = _SpyUnit(inner)
    group = ComponentGroup(unit, [Low(unit), High(unit)])  # type: ignore[list-item]
    await group.async_update()
    assert sorted(unit.reads) == [("holding", 0, 1), ("holding", 10, 1)]


async def test_group_still_merges_adjacent_members() -> None:
    """Members whose blocks touch are read as one: nothing is unclaimed."""

    class First(Component):
        a = integer(0)
        b = integer(1)

    class Second(Component):
        c = integer(2)
        d = integer(3)

    inner = MockModbusConnection().for_unit(1)
    unit = _SpyUnit(inner)
    group = ComponentGroup(unit, [First(unit), Second(unit)])  # type: ignore[list-item]
    await group.async_update()
    assert unit.reads == [("holding", 0, 4)]


async def test_group_merges_within_a_member_as_it_would_alone() -> None:
    """Gap planning inside a member is unchanged by pooling."""

    class Sparse(Component):
        a = integer(0)
        b = integer(8)  # within max_gap: one read
        c = integer(40)  # beyond it: its own read

    inner = MockModbusConnection().for_unit(1)
    unit = _SpyUnit(inner)
    group = ComponentGroup(unit, [Sparse(unit)])  # type: ignore[list-item]
    await group.async_update()
    assert sorted(unit.reads) == [("holding", 0, 9), ("holding", 40, 1)]


# -- diagnostics: raw registers keyed by address ------------------------------


class _Diag(Component):
    """Holding fields plus a coil, for exercising raw diagnostics."""

    count = integer(0, signed=False)
    energy = uint32(3, unit="Wh")  # spans 3..4
    relay = coil(0)


async def test_component_diagnostics_returns_raw_registers_by_address() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 7, 3: 0x0001, 4: 0x86A0})
    unit.coils[0] = True

    # No prior async_update: diagnostics reads the device fresh.
    raw = await _Diag(unit).async_read_raw()

    # count at 0, energy at 3..4 -> holding 0..4 pooled; the raw words land under
    # their absolute addresses, undecoded, keyed by their Modbus space.
    assert raw == {
        "holding": {0: 7, 1: 0, 2: 0, 3: 0x0001, 4: 0x86A0},
        "coil": {0: True},
    }


async def test_diagnostics_can_leave_listeners_alone() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 7, 3: 0x0001, 4: 0x86A0})
    unit.coils[0] = True
    diag = _Diag(unit)
    fired: list[int] = []
    diag.add_update_listener(lambda: fired.append(1))

    await diag.async_read_raw(notify=False)

    # The read is real, so the fields are current — only the listeners sat out.
    assert diag.count == 7
    assert not fired

    await diag.async_read_raw()
    assert fired == [1]


async def test_group_diagnostics_covers_every_space() -> None:
    class Holding(Component):
        a = integer(0, signed=False)

    class Input(Component):
        register_space = "input"
        b = integer(0, signed=False)

    class Bits(Component):
        on = coil(0)
        alarm = discrete_input(1)

    inner = MockModbusConnection().for_unit(1)
    inner.holding[0] = 11
    inner.input[0] = 22
    inner.coils[0] = True
    inner.discrete_inputs[1] = True
    unit = _SpyUnit(inner)

    group = ComponentGroup(unit, [Holding(unit), Input(unit), Bits(unit)])  # type: ignore[list-item]
    raw = await group.async_read_raw()

    assert raw == {
        "holding": {0: 11},
        "input": {0: 22},
        "coil": {0: True},
        "discrete": {1: True},
    }
    # Each space is read on its own; no space is skipped or merged into another.
    assert {space for space, _addr, _count in unit.reads} == {
        "holding",
        "input",
        "coil",
        "discrete",
    }


async def test_group_diagnostics_pools_adjacent_reads() -> None:
    inner = MockModbusConnection().for_unit(1)
    inner.holding.update({0: 1, 3: 0x0001, 4: 0x86A0})
    unit = _Counting(inner)
    group = ComponentGroup(unit, [Meter(unit)])  # type: ignore[list-item]

    await group.async_read_raw()

    # Meter's holding fields span 0..8 -> one pooled block, like async_update.
    assert unit.reads == [(0, 9)]


async def test_manual_component_diagnostics() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({5: 42, 6: 99})
    manual = ManualComponent(unit)
    manual.add("a", integer(5, signed=False))
    manual.add("b", integer(6, signed=False))

    raw = await manual.async_read_raw()

    assert raw == {"holding": {5: 42, 6: 99}}


async def test_diagnostics_raises_block_read_error() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.fail_read(0, ModbusExceptionError(2))  # illegal data address
    with pytest.raises(BlockReadError):
        await _Diag(unit).async_read_raw()


async def test_read_raw_refreshes_decoded_values_and_notifies() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 7, 3: 0x0001, 4: 0x86A0})
    dev = _Diag(unit)
    fired = 0

    def on_update() -> None:
        nonlocal fired
        fired += 1

    dev.add_update_listener(on_update)

    # A raw read advances the component like an update: decoded fields are
    # refreshed and listeners fire once.
    await dev.async_read_raw()
    assert dev.count == 7 and dev.energy == 100000
    assert fired == 1


async def test_group_read_raw_notifies_each_member_once() -> None:
    class Dev(Component):
        a = integer(0, signed=False)

    unit = MockModbusConnection().for_unit(1)
    unit.holding[0] = 5
    one, two = Dev(unit), Dev(unit)
    fired: list[str] = []
    one.add_update_listener(lambda: fired.append("one"))
    two.add_update_listener(lambda: fired.append("two"))

    await ComponentGroup(unit, [one, two]).async_read_raw()
    assert sorted(fired) == ["one", "two"]


async def test_read_raw_snapshot_replays_into_a_mock_via_load_raw() -> None:
    class Holding(Component):
        a = integer(0, signed=False)

    class Input(Component):
        register_space = "input"
        b = integer(0, signed=False)

    class Bits(Component):
        on = coil(0)
        alarm = discrete_input(1)

    # Capture a raw snapshot spanning all four spaces from one device.
    src = MockModbusConnection().for_unit(1)
    src.holding[0] = 11
    src.input[0] = 22
    src.coils[0] = True
    src.discrete_inputs[1] = True
    members = [Holding(src), Input(src), Bits(src)]
    raw = await ComponentGroup(src, members).async_read_raw()

    # Replay it into a fresh mock — no original device — and it reproduces itself.
    dst = MockModbusConnection().for_unit(1)
    dst.load_raw(raw)
    replayed = [Holding(dst), Input(dst), Bits(dst)]
    assert await ComponentGroup(dst, replayed).async_read_raw() == raw

    # And a component decodes the replayed registers just as against the device.
    holding = Holding(dst)
    await holding.async_update()
    assert holding.a == 11


async def test_load_raw_rejects_an_unknown_space() -> None:
    unit = MockModbusConnection().for_unit(1)
    # load_raw keys are the canonical spaces (async_read_raw's keys), e.g. "coil";
    # the store name "coils" is not one of them.
    with pytest.raises(ValueError, match="unknown space"):
        unit.load_raw({"coils": {0: True}})


# -- restrict_fields ----------------------------------------------------------


class _Boiler(Component):
    """The issue #80 shape: a declared range with a hole a device may refuse."""

    # Declared relative to the block start; resolves to (2000..2005), (2050) at
    # base_offset=2000 (see "Readable address ranges").
    register_ranges = ((0, 5), (50, 50))

    t0 = integer(0)
    t1 = integer(1)
    actual_high = integer(2)  # register 2002 — the one a controller may refuse
    t3 = integer(3)
    t4 = integer(4)
    t5 = integer(5)
    mode = integer(50, writable=True)


_SERVED = ("t0", "t1", "t3", "t4", "t5", "mode")  # every field except actual_high


def _boiler(refuse: int | None = None) -> tuple[_Boiler, _Counting]:
    inner = MockModbusConnection().for_unit(1)
    inner.holding.update(dict.fromkeys((2000, 2001, 2003, 2004, 2005, 2050), 0))
    if refuse is not None:
        inner.fail_read(refuse, ModbusExceptionError(2, "illegal data address"))
    unit = _Counting(inner)
    return _Boiler(unit, base_offset=2000), unit  # type: ignore[arg-type]


async def test_restrict_fields_issue_scenario_fails_without_it() -> None:
    # The baseline the issue describes: one refused register in the middle of a
    # declared range takes down the whole update.
    boiler, _ = _boiler(refuse=2002)
    with pytest.raises(BlockReadError):
        await boiler.async_update()


async def test_restrict_fields_splits_declared_range_around_dropped_register() -> None:
    boiler, unit = _boiler(refuse=2002)
    boiler.restrict_fields(_SERVED)
    await boiler.async_update()  # no BlockReadError — 2002 is never in a block

    read = {addr + i for addr, count in unit.reads for i in range(count)}
    assert 2002 not in read
    # The declared range is split at register 2 (abs 2002); the (50, 50)
    # isolation survives. Ranges stay in declared coordinates.
    assert boiler.register_ranges == ((0, 1), (3, 5), (50, 50))
    assert boiler.t0 == 0 and boiler.t5 == 0 and boiler.mode == 0
    assert boiler.actual_high is None  # dropped -> reads as None


async def test_restrict_fields_keeps_writes_working_and_drops_the_rest() -> None:
    boiler, _ = _boiler()
    boiler.restrict_fields(["mode"])
    await boiler.write("mode", 7)  # a kept writable field still writes
    with pytest.raises(AttributeError):
        await boiler.write("t0", 1)  # a dropped field can no longer be written


async def test_restrict_fields_synthesizes_ranges_when_none() -> None:
    class Gap(Component):  # no register_ranges -> gap-based planning
        a = integer(0)
        b = integer(1)
        c = integer(2)
        d = integer(3)

    inner = MockModbusConnection().for_unit(1)
    inner.holding.update({0: 10, 1: 11, 3: 13})
    inner.fail_read(2, ModbusExceptionError(2))
    unit = _Counting(inner)
    comp = Gap(unit)  # type: ignore[arg-type]
    comp.restrict_fields(["a", "b", "d"])  # drop c (register 2)
    await comp.async_update()  # would raise if any block spanned 2

    read = {addr + i for addr, count in unit.reads for i in range(count)}
    assert 2 not in read
    assert comp.register_ranges == ((0, 1), (3, 3))
    assert comp.a == 10 and comp.d == 13


async def test_restrict_fields_synthesized_ranges_cover_scale_registers() -> None:
    class Meter(Component):  # no register_ranges -> synthesized on restrict
        power = gauge(0, 1.0, scale_register=100)
        dropme = integer(1)

    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 5, 100: 1})  # raw 5, scale 10**1
    comp = Meter(unit)
    comp.restrict_fields(["power"])
    await comp.async_update()

    assert comp.register_ranges == ((0, 0), (100, 100))
    assert comp.power == 50


async def test_restrict_fields_synthesized_ranges_keep_a_shared_address() -> None:
    class Inverter(Component):  # no register_ranges -> synthesized on restrict
        grid_voltage = gauge(0, 0.1, unit="V")
        line_voltage_a_b = gauge(0, 0.1, unit="V")  # the same register, second name
        unserved = integer(1)
        active_power = int32(2)

    inner = MockModbusConnection().for_unit(1)
    inner.holding.update({0: 2300, 2: 0, 3: 5000})
    inner.fail_read(1, ModbusExceptionError(2))
    unit = _Counting(inner)
    comp = Inverter(unit)  # type: ignore[arg-type]
    comp.restrict_fields(["line_voltage_a_b", "active_power"])
    await comp.async_update()

    # 0 is kept under another name; only 1 is dropped outright.
    assert comp.register_ranges == ((0, 0), (2, 3))
    read = {addr + i for addr, count in unit.reads for i in range(count)}
    assert 1 not in read
    assert comp.line_voltage_a_b == 230.0
    assert comp.active_power == 5000
    assert comp.grid_voltage is None  # dropped -> reads as None


async def test_restrict_fields_declared_ranges_keep_a_shared_address() -> None:
    class Inverter(Component):
        register_ranges = ((0, 3),)

        grid_voltage = gauge(0, 0.1, unit="V")
        line_voltage_a_b = gauge(0, 0.1, unit="V")  # the same register, second name
        unserved = integer(1)
        active_power = int32(2)

    inner = MockModbusConnection().for_unit(1)
    inner.holding.update({0: 2300, 2: 0, 3: 5000})
    inner.fail_read(1, ModbusExceptionError(2))
    unit = _Counting(inner)
    comp = Inverter(unit)  # type: ignore[arg-type]
    comp.restrict_fields(["line_voltage_a_b", "active_power"])
    await comp.async_update()

    # Split at 1 only; 0 is still read under another name.
    assert comp.register_ranges == ((0, 0), (2, 3))
    assert comp.line_voltage_a_b == 230.0
    assert comp.active_power == 5000


async def test_restrict_fields_keeps_a_scale_register_a_dropped_field_names() -> None:
    class Scaled(Component):
        register_ranges = ((0, 1), (100, 100))

        power = gauge(0, 1.0, scale_register=100)
        dropme = integer(1)
        scale_raw = integer(100)  # the scale register, also exposed as a value

    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 5, 100: 1})  # raw 5, scale 10**1
    comp = Scaled(unit)
    comp.restrict_fields(["power"])
    await comp.async_update()

    # 100 is dropped as a field but is still the kept field's scale register.
    assert comp.register_ranges == ((0, 0), (100, 100))
    assert comp.power == 50


def test_restrict_fields_only_splits_never_merges_declared_ranges() -> None:
    class Dev(Component):
        # 4 is deliberately isolated (a wider block returns garbage for it).
        register_ranges = ((0, 2), (4, 4))
        a = integer(0)
        b = integer(1)
        c = integer(2)
        d = integer(4)

    comp = Dev(MockModbusConnection().for_unit(1))
    comp.restrict_fields(["a", "c", "d"])  # drop b (register 1)
    # (0, 2) is split around 1; (4, 4) is left untouched, never merged.
    assert comp.register_ranges == ((0, 0), (2, 2), (4, 4))


async def test_restrict_fields_narrows_bit_fields() -> None:
    class IO(Component):
        coil_ranges = ((0, 3),)
        a = coil(0)
        b = coil(1)
        c = coil(3)

    inner = MockModbusConnection().for_unit(1)
    inner.coils.update({0: True, 3: True})
    inner.fail_read(1, ModbusExceptionError(2), register_type="coil")
    io = IO(inner)
    io.restrict_fields(["a", "c"])  # drop coil b (address 1)
    await io.async_update()

    assert io.coil_ranges == ((0, 0), (2, 3))
    assert io.a is True and io.c is True and io.b is None


async def test_restrict_fields_can_be_called_after_first_update() -> None:
    boiler, _ = _boiler()
    await boiler.async_update()
    first_plan = boiler._plan
    assert boiler.actual_high == 0  # read on the first update

    boiler.restrict_fields(_SERVED)
    assert boiler._plan is None  # the cached plan is invalidated
    await boiler.async_update()

    assert boiler._plan is not first_plan  # re-planned from the narrowed fields
    assert boiler.actual_high is None  # its stale value was cleared


class _RestrictedMember(Component):
    """A group member whose middle field is dropped by restrict_fields."""

    a = integer(0)
    b = integer(2)  # the field restrict_fields drops


class _OtherMember(Component):
    c = integer(3)


def _restrict_group() -> tuple[
    _RestrictedMember, _OtherMember, ComponentGroup, _Counting
]:
    inner = MockModbusConnection().for_unit(1)
    inner.holding.update({0: 1, 1: 0, 3: 3})
    inner.fail_read(2, ModbusExceptionError(2))  # the device refuses register 2
    unit = _Counting(inner)
    one = _RestrictedMember(unit)  # type: ignore[arg-type]
    two = _OtherMember(unit)  # type: ignore[arg-type]
    return one, two, ComponentGroup(unit, [one, two]), unit  # type: ignore[list-item]


async def test_restrict_fields_after_pooling_reshapes_group_plan() -> None:
    one, two, group, unit = _restrict_group()
    one.restrict_fields(["a"])
    await group.async_update()  # would raise if any pooled block spanned 2

    read = {addr + i for addr, count in unit.reads for i in range(count)}
    assert 2 not in read
    assert one.a == 1 and two.c == 3
    assert one.b is None


async def test_restrict_fields_after_group_update_rebuilds_group_plan() -> None:
    inner = MockModbusConnection().for_unit(1)
    inner.holding.update({0: 1, 1: 0, 2: 9, 3: 3})
    unit = _Counting(inner)
    one = _RestrictedMember(unit)  # type: ignore[arg-type]
    two = _OtherMember(unit)  # type: ignore[arg-type]
    group = ComponentGroup(unit, [one, two])  # type: ignore[list-item]
    await group.async_update()
    assert one.b == 9  # read on the first update

    one.restrict_fields(["a"])
    unit.reads.clear()
    await group.async_update()

    read = {addr + i for addr, count in unit.reads for i in range(count)}
    assert 2 not in read  # the excluded register is no longer read
    assert one.b is None  # and its stale value stays cleared


def test_restrict_fields_rejects_unknown_name() -> None:
    boiler = _Boiler(MockModbusConnection().for_unit(1), base_offset=2000)
    with pytest.raises(ValueError, match="unknown field"):
        boiler.restrict_fields(["t0", "nope"])


def test_restrict_fields_rejects_repeating_group() -> None:
    class Sub(Component):
        v = integer(0)

    class Parent(Component):
        subs = repeating_group(2, Sub, stride=1)

    parent = Parent(MockModbusConnection().for_unit(1))
    with pytest.raises(ValueError, match="repeating_group"):
        parent.restrict_fields([])


# -- declared_fields ----------------------------------------------------------


class _Mixed(Component):
    """Registers and bits interleaved, to pin declaration order."""

    voltage = gauge(0, 0.1, unit="V")
    relay = coil(0, writable=True)
    energy = uint32(1, unit="Wh")
    fault = discrete_input(0)


def test_declared_fields_names_every_field_in_declaration_order() -> None:
    assert list(_Mixed.declared_fields) == ["voltage", "relay", "energy", "fault"]
    assert isinstance(_Mixed.declared_fields["relay"], CoilField)
    assert isinstance(_Mixed.declared_fields["fault"], DiscreteInputField)


def test_declared_fields_narrow_to_the_concrete_classes() -> None:
    """declared_fields is typed with the concrete classes, so values narrow."""
    kinds = {
        name: "register" if isinstance(field, RegisterField) else "bit"
        for name, field in _Mixed.declared_fields.items()
    }
    assert kinds == {
        "voltage": "register",
        "relay": "bit",
        "energy": "register",
        "fault": "bit",
    }
    assert _Mixed.declared_fields["relay"].space == "coil"
    assert _Mixed.declared_fields["fault"].space == "discrete"


def test_declared_fields_is_reachable_from_the_class_and_an_instance() -> None:
    component = _Mixed(MockModbusConnection().for_unit(1))
    assert component.declared_fields == _Mixed.declared_fields


def test_declared_fields_exposes_the_field_for_its_converter() -> None:
    assert _Mixed.declared_fields["voltage"] is _Mixed.__dict__["voltage"]


def test_declared_fields_survives_restrict_fields() -> None:
    boiler = _Boiler(MockModbusConnection().for_unit(1), base_offset=2000)
    before = list(boiler.declared_fields)
    boiler.restrict_fields(_SERVED)

    assert "actual_high" not in boiler._register_fields
    assert list(boiler.declared_fields) == before
    assert list(_Boiler.declared_fields) == before


def test_declared_fields_is_read_only() -> None:
    with pytest.raises(TypeError):
        _Mixed.declared_fields["voltage"] = None  # type: ignore[index]


def test_declared_fields_excludes_repeating_groups() -> None:
    class Sub(Component):
        v = integer(0)

    class Parent(Component):
        own = integer(0)
        subs = repeating_group(2, Sub, stride=1)

    assert list(Parent.declared_fields) == ["own"]


class _Scaling(Component):
    """Exists to be type-checked: a scaled field is not an integer one."""

    plain = uint32(0)
    scaled = uint32(2, scale=0.01)
    offsetted = uint32(4, offset=10.0)
    signed = int32(6)
    signed_scaled = int32(8, scale=0.001)
    word = integer(20)
    dynamic = integer(22, scale_register=23)


def _wants_int(value: int | None) -> None:
    """Accepts only what stays integral."""


def _wants_float(value: float | None) -> None:
    """Accepts what scaling makes fractional."""


def test_scaling_decides_the_declared_type_statically() -> None:
    # The body is the assertion — mypy fails this file if a scaled field goes
    # back to being typed int, which reads as integral and silently truncates
    # in the consumer rather than here.
    field = _Scaling(MockModbusConnection().for_unit(1))
    _wants_int(field.plain)
    _wants_int(field.signed)
    _wants_int(field.word)
    _wants_float(field.scaled)
    _wants_float(field.offsetted)
    _wants_float(field.signed_scaled)
    _wants_float(field.dynamic)
