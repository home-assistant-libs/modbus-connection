---
title: The device object
description: How the top-level device object of a library built on modbus-connection comes together.
---

modbus-connection is a foundation you build a device library on. A good
device library exposes one top-level object. A consumer constructs it from a
`ModbusUnit`, never from a connection or a host and port, and reads sub-systems
as plain Python attributes.

Each sub-system is a [`Component`](/modbus-connection/modelling/overview/). Some
are read once at setup: identity, model info, and whatever settles which
components this device serves. The rest are polled, grouped by category: what
the device measures, what it has been configured to do, and anything else that
needs its own interval. Give each category its own update method, so a consumer
chooses how often to read each. Read every sub-system on its own, or as a
[`ComponentGroup`](/modbus-connection/modelling/component-group/) where one's
read already spans the other's registers. One sub-system failing then does not
take the rest with it.

The example below is a heating controller:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from modbus_connection import (
    IllegalDataAddressError,
    IllegalFunctionError,
    ModbusConnectionError,
    ModbusError,
    ModbusTimeoutError,
)
from modbus_connection.model import Component, ComponentGroup

from .sensors import Sensors
from .controller import Controller
from .heating_circuit import HeatingCircuit
from .hot_water import HotWater
from .settings import Settings

if TYPE_CHECKING:
    from modbus_connection import ModbusUnit


async def _optional[C: Component](component: C) -> C | None:
    """Read an optional sub-system; None if this device does not have it."""
    try:
        await component.async_update()
    except (IllegalDataAddressError, IllegalFunctionError):
        return None
    return component


@dataclass
class UpdateReport:
    """What one poll managed to refresh."""

    updated: list[str] = field(default_factory=list)
    failed: dict[str, ModbusError] = field(default_factory=dict)


class MyDevice:
    """A heating controller reached through a ``ModbusUnit``."""

    def __init__(self, unit: ModbusUnit) -> None:
        self._unit = unit

        # Sub-systems, each a Component. Repeated ones take an index.
        self.controller = Controller(unit)
        self.sensors = Sensors(unit)
        self.heating_circuit_1 = HeatingCircuit(unit, index=1)
        self.settings = Settings(unit)

        # Optional: filled in by the first update if this model has them.
        self.heating_circuit_2: HeatingCircuit | None = None
        self.hot_water: HotWater | None = None

        # One circuit's read already spans the other's, so they read as one.
        self.circuits: ComponentGroup | None = None

        # class attributes of components that are updated together
        self._readings: tuple[str, ...] | None = None
        self._settings = ("settings",)

    async def _async_setup(self) -> None:
        """Read what never changes, and settle which sub-systems this model has.

        Runs from the first update, and again on the next one if the device
        was unreachable.
        """
        await self.controller.async_update()  # identity: read once, never polled

        # Probe to see which sub-systems this device has.
        self.heating_circuit_2 = await _optional(HeatingCircuit(self._unit, index=2))
        self.hot_water = await _optional(HotWater(self._unit))
        self.circuits = ComponentGroup(
            self._unit,
            [c for c in (self.heating_circuit_1, self.heating_circuit_2) if c],
        )

        self._readings = tuple(
            n
            for n in ("sensors", "circuits", "hot_water")
            if getattr(self, n) is not None
        )

    async def _async_poll(
        self, names: tuple[str, ...], report: UpdateReport
    ) -> UpdateReport:
        """Read each named sub-system on its own, adding what happened to *report*."""
        for name in names:
            try:
                await getattr(self, name).async_update(notify=False)
            except ModbusConnectionError:
                raise  # the link is down; the rest would only wait for timeouts
            except ModbusTimeoutError as err:
                if not report.updated and not report.failed:
                    raise  # nothing answered yet: assume the rest time out too
                report.failed[name] = err
            except ModbusError as err:
                report.failed[name] = err
            else:
                report.updated.append(name)
        return report

    def _notify(self, report: UpdateReport) -> None:
        """Fire the listeners of everything this update refreshed."""
        for name in report.updated:
            getattr(self, name).notify()

    async def async_update_readings(self) -> UpdateReport:
        """Refresh what the controller measures."""
        if self._readings is None:
            await self._async_setup()
            assert self._readings is not None
        report = await self._async_poll(self._readings, UpdateReport())
        self._notify(report)
        return report

    async def async_update_settings(self) -> UpdateReport:
        """Refresh what the controller has been configured to do."""
        if self._readings is None:
            await self._async_setup()
        report = await self._async_poll(self._settings, UpdateReport())
        self._notify(report)
        return report

    async def async_update(self) -> UpdateReport:
        """Refresh all components."""
        if self._readings is None:
            await self._async_setup()
            assert self._readings is not None
        report = await self._async_poll(self._readings, UpdateReport())
        await self._async_poll(self._settings, report)
        self._notify(report)
        return report

    async def async_read_raw(self) -> dict[str, dict[int, int | bool]]:
        """Every register this device reads, undecoded, for diagnostics."""
        if self._readings is None:
            await self._async_setup()
            assert self._readings is not None
        raw: dict[str, dict[int, int | bool]] = {}
        for name in ("controller", *self._readings, *self._settings):
            read = await getattr(self, name).async_read_raw(notify=False)
            for space, values in read.items():
                raw.setdefault(space, {}).update(values)
        return raw
```

The consumer then works entirely in Python objects:

```python
import asyncio
from modbus_connection import ModbusSerialParams
from modbus_connection.tmodbus import ModbusConnection
from my_device import MyDevice


async def main() -> None:
    connection = ModbusConnection(
        ModbusSerialParams(device="socket://192.168.1.50:8899")
    )
    try:
        unit = connection.for_unit(246)
        device = MyDevice(unit)
        await device.async_update()

        print("Outside temperature:", device.sensors.outside_1)
        print("Circuit 1 setpoint:", device.heating_circuit_1.room_setpoint_day)
        if device.hot_water is not None:  # absent on some models
            print("Hot water:", device.hot_water.temperature)
    finally:
        await connection.close()


asyncio.run(main())
```

## Principles

- Take a `ModbusUnit`. The consumer owns and closes the link. Your library
  only reads and writes registers. This keeps the library backend-neutral, so
  it works over tmodbus, pymodbus, or the mock unchanged.
- Model one sub-system per `Component`. Group registers by function and give
  each its own file. This keeps the address map readable and lets a sub-system
  refresh alone.
- Ask for the timing your device needs. A device slow to answer says so on its
  unit, with
  [`require_timeout()` and `require_connect_delay()`](/modbus-connection/connection/connections-and-units/#device-requirements),
  and [`set_message_spacing()`](/modbus-connection/connection/connections-and-units/#request-spacing)
  for a gap between its own frames. Whoever builds the connection cannot know
  this. Your library can.
- Carry metadata on the fields. `unit=`, ranges, and validators live next to
  the address, so the model doubles as the datasheet.
- Decide at setup. Everything that cannot change between two polls (the model,
  the static registers, which optional components exist) belongs to setup, so
  the polling path stays a fixed list of components to read.
- Split where the blocks divide. Give the settings their own update method
  when they sit in blocks of their own.

## A library built this way

[sofar-modbus](https://github.com/darkrain-nl/sofar-modbus) follows this page end
to end. Its
[`SofarInverter`](https://github.com/darkrain-nl/sofar-modbus/blob/main/src/sofar_modbus/modern/device.py)
takes a `ModbusUnit`. It holds one `Component` per sub-system, such as the
[PV strings](https://github.com/darkrain-nl/sofar-modbus/blob/main/src/sofar_modbus/modern/pv.py).
Setup settles which sub-systems this inverter serves. The poll then splits into
`async_update_readings()` and `async_update_settings()`, and each returns an
`UpdateReport`. The
[`sofar`](https://github.com/home-assistant/core/tree/dev/homeassistant/components/sofar)
integration in Home Assistant consumes it.
