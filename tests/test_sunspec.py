"""Tests for the SunSpec field factories (modbus_connection.model.sunspec)."""

from __future__ import annotations

import logging
import struct
from enum import IntEnum, IntFlag

import pytest

from modbus_connection.mock import MockModbusConnection
from modbus_connection.model import Component
from modbus_connection.model import sunspec as ss


class Mode(IntEnum):
    OFF = 0
    HEAT = 2
    COOL = 3


class Events(IntFlag):
    OVERTEMP = 1
    DOOR_OPEN = 2


class Inverter(Component):
    """A small slice of a SunSpec inverter model."""

    a = ss.uint16(0, scale_register=1)  # AC current, scaled by A_SF
    a_sf = ss.sunssf(1)
    power = ss.int16(2, scale_register=3, unit="W")
    power_sf = ss.sunssf(3)
    wh = ss.acc32(4, unit="Wh")  # lifetime energy
    status = ss.enum16(6)
    events = ss.bitfield32(7)
    serial = ss.string(9, 4)


def _inverter(values: dict[int, int]) -> Inverter:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update(values)
    return Inverter(unit)


async def test_dynamic_scale_factor() -> None:
    # current 1234 * 10**-2 = 12.34 A; power 250 * 10**1 = 2500 W
    inv = _inverter(
        {0: 1234, 1: (-2) & 0xFFFF, 2: 250, 3: 1, 4: 0x0001, 5: 0x86A0, 6: 4}
    )
    await inv.async_update()
    assert inv.a == pytest.approx(12.34)
    assert inv.power == 2500
    assert inv.wh == 100000
    assert inv.status == 4


async def test_unimplemented_values_decode_to_none() -> None:
    inv = _inverter(
        {
            0: 0xFFFF,  # uint16 unimplemented
            2: 0x8000,  # int16 unimplemented
            4: 0x0000,
            5: 0x0000,  # acc32 not accumulated
            6: 0xFFFF,  # enum16 unimplemented
            7: 0xFFFF,
            8: 0xFFFF,  # bitfield32 unimplemented
        }
    )
    await inv.async_update()
    assert inv.a is None
    assert inv.power is None
    assert inv.wh is None
    assert inv.status is None
    assert inv.events is None


@pytest.mark.parametrize("scale_factor", [11, -11, 100, -32768])
async def test_out_of_spec_scale_factor_decodes_to_none(scale_factor: int) -> None:
    """A sunssf exponent outside the spec's -10..10 decodes the value to None.

    Devices have been seen reporting garbage exponents (typically around an
    inverter's sleep/wake transition); scaling a sane raw value by one would
    turn it into an absurd reading, so the value is unknown instead.
    """
    inv = _inverter({0: 1234, 1: scale_factor & 0xFFFF, 2: 250, 3: 1})
    await inv.async_update()
    assert inv.a is None
    assert inv.power == 2500  # its own exponent is fine


async def test_spec_range_boundary_scale_factors_compute() -> None:
    """The spec range is inclusive: -10 and 10 still scale normally."""
    inv = _inverter({0: 2, 1: 10, 2: 2, 3: (-10) & 0xFFFF})
    await inv.async_update()
    assert inv.a == 2 * 10**10
    assert inv.power == pytest.approx(2e-10)


async def test_accumulator_out_of_spec_scale_factor_decodes_to_none() -> None:
    """Accumulators bound their dynamic exponent to the sunssf range too."""

    class Accumulating(Component):
        wh = ss.acc32(0, scale_register=2, unit="Wh")
        wh_sf = ss.sunssf(2)

    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 0x0001, 1: 0x86A0, 2: 11})
    comp = Accumulating(unit)
    await comp.async_update()
    assert comp.wh is None


async def test_write_with_out_of_spec_scale_factor_raises() -> None:
    """A write never scales by an out-of-spec exponent; it raises instead.

    Unlike a read, which decodes such a value to None, a write must never
    guess, so nothing may reach the device.
    """

    class Control(Component):
        limit = ss.uint16(0, scale_register=1, writable=True)

    unit = MockModbusConnection().for_unit(1)
    unit.holding[1] = 11
    with pytest.raises(ValueError, match="outside the spec range"):
        await Control(unit).write("limit", 100)
    assert 0 not in unit.holding  # nothing was written


async def test_accumulator_dynamic_scale_factor() -> None:
    # SunSpec scales accumulators dynamically too (model 103's WH via WH_SF).
    class Accumulating(Component):
        wh = ss.acc32(0, scale_register=2, unit="Wh")
        wh_sf = ss.sunssf(2)

    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 0x0001, 1: 0x86A0, 2: (-2) & 0xFFFF})
    comp = Accumulating(unit)
    await comp.async_update()
    assert comp.wh == pytest.approx(1000.0)  # 100000 * 10**-2
    assert comp.wh_sf == -2


async def test_string_field() -> None:
    inv = _inverter({9: 0x4142, 10: 0x4344, 11: 0x4546, 12: 0x0000})
    await inv.async_update()
    assert inv.serial == "ABCDEF"


async def test_float_unimplemented_is_none() -> None:
    class WithFloat(Component):
        freq = ss.float32(0)

    unit = MockModbusConnection().for_unit(1)
    hi, lo = struct.unpack(">HH", struct.pack(">f", float("nan")))
    unit.holding.update({0: hi, 1: lo})
    comp = WithFloat(unit)
    await comp.async_update()
    assert comp.freq is None


def test_all_factories_build_fields() -> None:
    # Every exported field factory produces a usable RegisterField.
    discovery = {
        "SunSpecComponent",
        "SunSpecError",
        "SunSpecMapShiftError",
        "SunSpecModel",
        "SunSpecModels",
        "scan",
    }
    for name in set(ss.__all__) - discovery:
        factory = getattr(ss, name)
        field = factory(0, 4) if name == "string" else factory(0)
        assert field.count >= 1


# -- native enum / bitfield mapping -------------------------------------------


class Device(Component):
    mode = ss.enum16(0, Mode, writable=True)
    events = ss.bitfield16(1, Events)
    raw_mode = ss.enum16(2)  # bare form keeps the raw int


def _device(values: dict[int, int]) -> Device:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update(values)
    return Device(unit)


async def test_enum_and_bitfield_decode_to_members() -> None:
    dev = _device({0: 2, 1: 0b11, 2: 5})
    await dev.async_update()
    assert dev.mode is Mode.HEAT
    assert dev.mode == 2  # IntEnum stays int-comparable
    assert dev.events == Events.OVERTEMP | Events.DOOR_OPEN
    assert dev.events & Events.OVERTEMP
    assert dev.raw_mode == 5  # no enum passed -> raw int


async def test_enum_write_accepts_member() -> None:
    dev = _device({})
    await dev.write("mode", Mode.COOL)
    assert (await dev._unit.read_holding_registers(0, 1))[0] == 3


async def test_unknown_enum_decodes_to_none_and_warns_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    dev = _device({0: 7})  # 7 is not a Mode member
    with caplog.at_level(logging.WARNING, logger="modbus_connection.model"):
        await dev.async_update()
        assert dev.mode is None
        await dev.async_update()  # second poll: still None, no second warning
        assert dev.mode is None
    warnings = [r for r in caplog.records if "no mapping for value 7" in r.message]
    assert len(warnings) == 1


async def test_unknown_bitfield_bits_are_kept() -> None:
    dev = _device({1: 0xFFF0})  # unknown high bits, IntFlag KEEP boundary
    await dev.async_update()
    assert dev.events is not None
    assert int(dev.events) == 0xFFF0


# -- boolean points -------------------------------------------------------------


class Switch(Component):
    enabled = ss.boolean(0, writable=True)
    backup = ss.boolean(1)


async def test_boolean_decodes_codes() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 1, 1: 0})
    dev = Switch(unit)
    await dev.async_update()
    assert dev.enabled is True
    assert dev.backup is False


async def test_boolean_unimplemented_is_none() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 0xFFFF, 1: 1})
    dev = Switch(unit)
    await dev.async_update()
    assert dev.enabled is None
    assert dev.backup is True


async def test_boolean_out_of_spec_code_is_none() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 2, 1: 1})  # 2 is neither 0 nor 1
    dev = Switch(unit)
    await dev.async_update()
    assert dev.enabled is None


async def test_boolean_write() -> None:
    unit = MockModbusConnection().for_unit(1)
    dev = Switch(unit)
    await dev.write("enabled", True)
    assert (await unit.read_holding_registers(0, 1))[0] == 1
    await dev.write("enabled", False)
    assert (await unit.read_holding_registers(0, 1))[0] == 0


# -- model discovery -----------------------------------------------------------


def _chain(base: int) -> dict[int, int]:
    """A map with a Common-like model (len 4) and a second model (len 2)."""
    return {
        base: 0x5375,  # "Su"
        base + 1: 0x6E53,  # "nS"
        base + 2: 1,  # model 1 header
        base + 3: 4,
        base + 4: 0x4142,  # "AB"
        base + 5: 0,
        base + 6: 0,
        base + 7: 0,
        base + 8: 103,  # model 103 header
        base + 9: 2,
        base + 10: 1234,
        base + 11: (-1) & 0xFFFF,
        base + 12: 0xFFFF,  # end marker
        base + 13: 0,
    }


async def test_scan() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update(_chain(1000))
    models = await ss.scan(unit, 1000)
    assert models == {
        1: [ss.SunSpecModel(model_id=1, address=1002, length=4)],
        103: [ss.SunSpecModel(model_id=103, address=1008, length=2)],
    }


async def test_scan_repeated_model_id() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update(
        {
            0: 0x5375,
            1: 0x6E53,
            2: 203,  # two meters of the same model
            3: 1,
            4: 0,
            5: 203,
            6: 1,
            7: 0,
            8: 0xFFFF,
            9: 0,
        }
    )
    models = await ss.scan(unit, 0)
    assert models == {
        203: [
            ss.SunSpecModel(model_id=203, address=2, length=1),
            ss.SunSpecModel(model_id=203, address=5, length=1),
        ]
    }


def _multi_model_chain() -> dict[int, int]:
    """A SolarEdge-shaped chain: an inverter, then two identity/meter pairs."""
    registers = {0: 0x5375, 1: 0x6E53}
    address = 2
    for model_id, length in ((1, 4), (103, 2), (1, 4), (203, 1), (1, 4), (201, 1)):
        registers[address] = model_id
        registers[address + 1] = length
        address += 2 + length
    registers[address] = 0xFFFF
    registers[address + 1] = 0
    return registers


async def test_scan_result_chain_is_in_chain_order() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update(_multi_model_chain())
    models = await ss.scan(unit, 0)
    assert [(model.model_id, model.address) for model in models.chain] == [
        (1, 2),
        (103, 8),
        (1, 12),
        (203, 18),
        (1, 21),
        (201, 27),
    ]


async def test_scan_result_chain_pairs_a_meter_with_its_identity() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update(_multi_model_chain())
    chain = (await ss.scan(unit, 0)).chain
    # Each meter is the model after an identity block, and the two meters
    # differ in model ID — so only the order distinguishes their identities.
    meters = [
        (identity.address, meter.model_id)
        for identity, meter in zip(chain, chain[1:], strict=False)
        if identity.model_id == 1 and meter.model_id != 103
    ]
    assert meters == [(12, 203), (21, 201)]


async def test_scan_result_at_locates_a_model_by_address() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update(_multi_model_chain())
    models = await ss.scan(unit, 0)
    assert models.at(18) == ss.SunSpecModel(model_id=203, address=18, length=1)
    assert models.at(19) is None  # inside the block, not its header


def test_model_span_covers_the_header() -> None:
    model = ss.SunSpecModel(model_id=203, address=18, length=1)
    assert model.span == 3
    assert model.address + model.span == 21  # the next model's header


async def test_scan_result_first_prefers_argument_order() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update(_chain(1000))
    models = await ss.scan(unit, 1000)
    assert isinstance(models, dict)  # still usable as a plain dict
    # 111 is absent; 103 is tried before the also-present 1
    assert models.first(111, 103, 1) == ss.SunSpecModel(
        model_id=103, address=1008, length=2
    )
    assert models.first(111, 112) is None


async def test_scan_result_first_takes_chain_order_within_an_id() -> None:
    models = ss.SunSpecModels(
        {
            203: [
                ss.SunSpecModel(model_id=203, address=2, length=1),
                ss.SunSpecModel(model_id=203, address=5, length=1),
            ]
        }
    )
    assert models.first(203) == ss.SunSpecModel(model_id=203, address=2, length=1)


async def test_scan_no_marker() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({40000: 0, 40001: 0})
    with pytest.raises(ss.SunSpecError, match="No SunSpec marker"):
        await ss.scan(unit, 40000)


async def test_scan_unterminated_chain() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 0x5375, 1: 0x6E53})  # models read as id 0, len 0
    with pytest.raises(ss.SunSpecError, match="not terminated"):
        await ss.scan(unit, 0)


class _Discovered(ss.SunSpecComponent):
    value = ss.uint16(2, scale_register=3)
    value_sf = ss.sunssf(3)


async def test_sunspec_component_at_discovered_model() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update(_chain(1000))
    (model,) = (await ss.scan(unit, 1000))[103]
    component = _Discovered(unit, model)
    await component.async_update()
    assert component.value == pytest.approx(123.4)  # 1234 * 10**-1


async def test_restrict_fields_keeps_the_model_header() -> None:
    """The header is read whatever the restriction says, so updates still pass."""
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update(_chain(1000))
    (model,) = (await ss.scan(unit, 1000))[103]
    component = _Discovered(unit, model)
    component.restrict_fields(["value", "value_sf"])  # header not listed
    await component.async_update()
    assert component.value == pytest.approx(123.4)
    assert component.model_id == 103


async def test_restrict_fields_still_detects_a_shifted_map() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update(_chain(1000))
    (model,) = (await ss.scan(unit, 1000))[103]
    component = _Discovered(unit, model)
    component.restrict_fields(["value", "value_sf"])
    unit.holding[1008] = 111  # the map shifts
    with pytest.raises(ss.SunSpecMapShiftError, match="header mismatch"):
        await component.async_update()


async def test_restrict_fields_drops_the_fields_it_excludes() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update(_chain(1000))
    (model,) = (await ss.scan(unit, 1000))[103]
    component = _Discovered(unit, model)
    component.restrict_fields(["value_sf"])
    await component.async_update()
    assert component.value is None  # excluded, and the header did not save it


async def test_sunspec_component_header_check() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update(_chain(1000))
    (model,) = (await ss.scan(unit, 1000))[103]
    component = _Discovered(unit, model)
    # the map shifts: another model now sits at the old address
    unit.holding[1008] = 111
    with pytest.raises(ss.SunSpecMapShiftError, match="header mismatch"):
        await component.async_update()


async def test_sunspec_component_header_check_in_group() -> None:
    from modbus_connection.model import ComponentGroup

    unit = MockModbusConnection().for_unit(1)
    unit.holding.update(_chain(1000))
    (model,) = (await ss.scan(unit, 1000))[103]
    component = _Discovered(unit, model)
    unit.holding[1009] = 3  # the model's length changed
    with pytest.raises(ss.SunSpecMapShiftError, match="header mismatch"):
        await ComponentGroup(unit, [component]).async_update()


async def test_sunspec_header_check_runs_without_notify() -> None:
    """The map-shift check is part of the read, not of notifying."""
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update(_chain(1000))
    (model,) = (await ss.scan(unit, 1000))[103]
    component = _Discovered(unit, model)
    unit.holding[1008] = 111  # the map shifts
    with pytest.raises(ss.SunSpecMapShiftError, match="header mismatch"):
        await component.async_update(notify=False)


async def test_sunspec_group_header_check_precedes_all_listeners() -> None:
    """A shifted member fails the group update before any member notifies."""
    from modbus_connection.model import ComponentGroup

    class _Common(ss.SunSpecComponent):
        serial = ss.string(2, 2)

    unit = MockModbusConnection().for_unit(1)
    unit.holding.update(_chain(1000))
    models = await ss.scan(unit, 1000)
    healthy = _Common(unit, models[1][0])
    shifted = _Discovered(unit, models[103][0])
    fired: list[str] = []
    healthy.add_update_listener(lambda: fired.append("healthy"))
    unit.holding[1009] = 3  # 103's length changed; model 1 is untouched
    with pytest.raises(ss.SunSpecMapShiftError, match="header mismatch"):
        await ComponentGroup(unit, [healthy, shifted]).async_update()
    assert fired == []  # the healthy member's listeners never fired
