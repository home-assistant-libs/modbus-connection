"""Tests for the SunSpec field factories (modbus_connection.model.sunspec)."""

from __future__ import annotations

import struct

import pytest

from modbus_connection.mock import MockModbusConnection
from modbus_connection.model import Component
from modbus_connection.model import sunspec as ss


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
