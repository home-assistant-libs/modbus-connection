"""Tests for the SunSpec field factories (modbus_connection.model.sunspec)."""

from __future__ import annotations

import logging
import struct
from enum import IntEnum, IntFlag

import pytest

import modbus_connection.model as model
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
    # Every exported factory produces a usable RegisterField.
    for name in ss.__all__:
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
    model._warned_unknown_enum.clear()
    dev = _device({0: 7})  # 7 is not a Mode member
    with caplog.at_level(logging.WARNING, logger="modbus_connection.model"):
        await dev.async_update()
        assert dev.mode is None
        await dev.async_update()  # second poll: still None, no second warning
        assert dev.mode is None
    warnings = [r for r in caplog.records if "no member for value 7" in r.message]
    assert len(warnings) == 1


async def test_unknown_bitfield_bits_are_kept() -> None:
    dev = _device({1: 0xFFF0})  # unknown high bits, IntFlag KEEP boundary
    await dev.async_update()
    assert dev.events is not None
    assert int(dev.events) == 0xFFF0
