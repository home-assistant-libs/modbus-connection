"""Tests for the ``Device`` base class (modbus_connection.model.device)."""

from __future__ import annotations

import pytest

from modbus_connection.exceptions import (
    IllegalDataAddressError,
    IllegalDataValueError,
    IllegalFunctionError,
    ModbusConnectionError,
    ModbusError,
    ModbusTimeoutError,
)
from modbus_connection.mock import MockModbusUnit
from modbus_connection.model import (
    Component,
    Device,
    UpdateReport,
    integer,
    read_optional,
)

IDENTITY_ADDRESS = 0
SENSORS_ADDRESS = 10
HOT_WATER_ADDRESS = 20
SETTINGS_ADDRESS = 30


class Identity(Component):
    model = integer(IDENTITY_ADDRESS)


class Sensors(Component):
    temperature = integer(SENSORS_ADDRESS)


class HotWater(Component):
    temperature = integer(HOT_WATER_ADDRESS)


class Settings(Component):
    setpoint = integer(SETTINGS_ADDRESS)


class Heater(Device):
    """A device with one identity read at setup and one optional sub-system."""

    def __init__(self, unit: MockModbusUnit) -> None:
        super().__init__(unit)
        self.identity = Identity(unit)
        self.sensors = Sensors(unit)
        self.settings = Settings(unit)
        self.hot_water: HotWater | None = None
        self.setups = 0

    async def _async_setup(self) -> None:
        self.setups += 1
        await self.identity.async_update()
        self.hot_water = await read_optional(HotWater(self.modbus_unit))


@pytest.fixture
def heater(mock_modbus_unit: MockModbusUnit) -> Heater:
    mock_modbus_unit.holding[IDENTITY_ADDRESS] = 7
    mock_modbus_unit.holding[SENSORS_ADDRESS] = 215
    mock_modbus_unit.holding[HOT_WATER_ADDRESS] = 480
    mock_modbus_unit.holding[SETTINGS_ADDRESS] = 21
    return Heater(mock_modbus_unit)


async def test_setup_runs_once(heater: Heater) -> None:
    await heater.async_poll(("sensors",))
    await heater.async_poll(("settings",))
    await heater.async_read_raw(("sensors",))

    assert heater.setups == 1
    assert heater.identity.model == 7
    assert heater.hot_water is not None
    assert heater.hot_water.temperature == 480


async def test_setup_reruns_after_failure(
    mock_modbus_unit: MockModbusUnit, heater: Heater
) -> None:
    mock_modbus_unit.fail_requests(ModbusTimeoutError())
    with pytest.raises(ModbusTimeoutError):
        await heater.async_poll(("sensors",))
    assert heater.setups == 1

    mock_modbus_unit.fail_requests(None)
    report = await heater.async_poll(("sensors",))
    assert heater.setups == 2
    assert report.updated == ["sensors"]


async def test_poll_updates_and_notifies(heater: Heater) -> None:
    fired: list[str] = []
    heater.sensors.add_update_listener(lambda: fired.append("sensors"))
    heater.settings.add_update_listener(lambda: fired.append("settings"))

    report = await heater.async_poll(("sensors", "settings"))

    assert report == UpdateReport(updated=["sensors", "settings"])
    assert heater.sensors.temperature == 215
    assert heater.settings.setpoint == 21
    assert fired == ["sensors", "settings"]


async def test_poll_connection_error_reraises(
    mock_modbus_unit: MockModbusUnit, heater: Heater
) -> None:
    mock_modbus_unit.fail_read(SETTINGS_ADDRESS, ModbusConnectionError())
    with pytest.raises(ModbusConnectionError):
        await heater.async_poll(("sensors", "settings"))


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ModbusTimeoutError(), id="timeout_after_an_answer"),
        pytest.param(IllegalDataAddressError(), id="device_rejected_the_block"),
    ],
)
async def test_poll_records_a_failed_sub_system(
    mock_modbus_unit: MockModbusUnit, heater: Heater, error: ModbusError
) -> None:
    fired: list[str] = []
    heater.settings.add_update_listener(lambda: fired.append("settings"))
    mock_modbus_unit.fail_read(SETTINGS_ADDRESS, error)

    report = await heater.async_poll(("sensors", "settings"))

    assert report.updated == ["sensors"]
    assert list(report.failed) == ["settings"]
    assert isinstance(report.failed["settings"], type(error))
    assert fired == []


async def test_poll_timeout_before_any_answer_reraises(
    mock_modbus_unit: MockModbusUnit, heater: Heater
) -> None:
    mock_modbus_unit.fail_read(SENSORS_ADDRESS, ModbusTimeoutError())
    with pytest.raises(ModbusTimeoutError):
        await heater.async_poll(("sensors", "settings"))


async def test_poll_adds_to_an_earlier_report(
    mock_modbus_unit: MockModbusUnit, heater: Heater
) -> None:
    mock_modbus_unit.fail_read(SETTINGS_ADDRESS, ModbusTimeoutError())
    report = await heater.async_poll(("sensors",))

    report = await heater.async_poll(("settings",), report)

    assert report.updated == ["sensors"]
    assert isinstance(report.failed["settings"], ModbusTimeoutError)


@pytest.mark.parametrize(
    "error", [IllegalDataAddressError(), IllegalFunctionError()], ids=type
)
async def test_read_optional_refused(
    mock_modbus_unit: MockModbusUnit, error: ModbusError
) -> None:
    mock_modbus_unit.fail_read(HOT_WATER_ADDRESS, error)
    assert await read_optional(HotWater(mock_modbus_unit)) is None


async def test_read_optional_present(mock_modbus_unit: MockModbusUnit) -> None:
    mock_modbus_unit.holding[HOT_WATER_ADDRESS] = 480
    hot_water = HotWater(mock_modbus_unit)
    assert await read_optional(hot_water) is hot_water
    assert hot_water.temperature == 480


async def test_read_optional_propagates_other_errors(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    mock_modbus_unit.fail_read(HOT_WATER_ADDRESS, IllegalDataValueError())
    with pytest.raises(IllegalDataValueError):
        await read_optional(HotWater(mock_modbus_unit))


async def test_read_raw_merges_components(heater: Heater) -> None:
    fired: list[str] = []
    heater.sensors.add_update_listener(lambda: fired.append("sensors"))

    raw = await heater.async_read_raw(("sensors", "settings"))

    assert raw == {"holding": {SENSORS_ADDRESS: 215, SETTINGS_ADDRESS: 21}}
    assert fired == []


async def test_absent_sub_system_is_skipped(
    mock_modbus_unit: MockModbusUnit, heater: Heater
) -> None:
    mock_modbus_unit.fail_read(HOT_WATER_ADDRESS, IllegalDataAddressError())

    report = await heater.async_poll(("sensors", "hot_water"))
    raw = await heater.async_read_raw(("sensors", "hot_water"))

    assert heater.hot_water is None
    assert report == UpdateReport(updated=["sensors"])
    assert raw == {"holding": {SENSORS_ADDRESS: 215}}
