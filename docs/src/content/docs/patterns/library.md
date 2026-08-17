---
title: The device object
description: How the top-level device object of a library built on modbus-connection comes together.
---

modbus-connection is a foundation you build a **device library** on. A good device
library exposes one **top-level object** that a consumer constructs from a
`ModbusUnit` — never a connection, and never a host/port — and reads sub-systems as
plain Python attributes.

Each sub-system is a [`Component`](/modbus-connection/modelling/overview/). Some
are read once at setup: identity, model info, and whatever settles which
components this device serves. The rest are polled, grouped by category — what the
device measures, what it has been configured to do, anything else worth its own
interval — with [an update method per category](#readings-and-settings), so a
consumer chooses how often to read each. Every sub-system is read on its own, or
as a [`ComponentGroup`](/modbus-connection/modelling/component-group/) where one's
read already spans the other's registers, so one that fails does not take the rest
with it.

Over a heating controller:

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
        # Configured, not measured: the schedule and the mode selectors.
        self.settings = Settings(unit)
        # Optional: filled in by the first update if this model has them.
        self.heating_circuit_2: HeatingCircuit | None = None
        self.hot_water: HotWater | None = None
        # One circuit's read already spans the other's, so they read as one.
        self.circuits: ComponentGroup | None = None

        self._readings: tuple[str, ...] | None = None
        self._settings = ("settings",)

    async def _async_setup(self) -> None:
        """Read what never changes, and settle which sub-systems this model has.

        Runs from the first ``async_update()``, and again on the next one if
        the device was unreachable.
        """
        await self.controller.async_update()  # identity: read once, never polled

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
        """Refresh what the controller measures; the first call sets it up."""
        if self._readings is None:
            await self._async_setup()
        assert self._readings is not None  # _async_setup() always builds it
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
        """Both, for a caller that does not schedule them apart."""
        if self._readings is None:
            await self._async_setup()
        assert self._readings is not None  # _async_setup() always builds it
        report = await self._async_poll(self._readings, UpdateReport())
        await self._async_poll(self._settings, report)
        self._notify(report)  # nothing fires until the whole cycle is done
        return report

    async def async_read_raw(self) -> dict[str, dict[int, int | bool]]:
        """Every register this device reads, undecoded — for diagnostics."""
        raw: dict[str, dict[int, int | bool]] = {}
        for name in ("controller", *(self._readings or ()), *self._settings):
            read = await getattr(self, name).async_read_raw(notify=False)
            for space, values in read.items():
                raw.setdefault(space, {}).update(values)
        return raw
```

The consumer then works entirely in Python objects:

```python
import asyncio
from modbus_connection import ModbusTcpParams
from modbus_connection.tmodbus import ModbusConnection
from my_device import MyDevice


async def main() -> None:
    connection = ModbusConnection(
        ModbusTcpParams(host="192.168.1.50", port=502, framer="rtu")
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

## Readings and settings

What a device measures changes constantly. What it has been configured to do
changes when something writes it. A consumer can only poll the second less often
than the first if the device object says which is which — hence the two methods
above, and `async_update()` for a caller that does not care. Two is the common
split, not a rule: a set of components a consumer would schedule differently again
earns a third method on the same terms.

`_async_poll` takes the report instead of making one, which is what keeps the
fatal-timeout rule honest: nothing answered has to mean nothing answered *this
cycle*, so a settings poll on its own still gives up on its first timeout while
`async_update()` gives up only if its very first block does. Notifying is separate
for the same reason — `_notify(report)` fires once, at the end of whichever call
the consumer made, so `async_update()` still fires nothing until the whole cycle
is done. Both are what keep it the method it was before the split.

Name the methods for what they read, never for when to call them — a library
cannot know a consumer's schedule, and `async_update_slow()` is wrong the moment
someone wants it now. Each report names only what its own method polled, so a
component absent from one is not a component that failed.

Whether to split at all is yours to judge. It pays where the configuration
registers read in blocks of their own; where they interleave with measurements,
planning the two halves apart can cost more requests than one poll. A component
that mixes the two cannot move — carve it in half first, or leave it. And a split
that cannot take every setting with it is worse than none: a caller would write a
setting, refresh, and not read it back.

Sibling classes need not match. Where a model's map is measurement only, that
class keeps its single `async_update()` rather than an empty settings poll.

## Principles

- **Take a `ModbusUnit`, not a connection.** The consumer owns and closes the
  link; your library only reads and writes registers. This keeps the library
  backend-neutral — it works over tmodbus, pymodbus, or the mock unchanged.
- **One sub-system per `Component`.** Group registers by function; give each its
  own file. It keeps the address map readable and lets a sub-system refresh alone.
- **Carry metadata on the fields.** `unit=`, ranges, and validators live next to
  the address, so the model *is* the datasheet.
- **Decide once, poll forever.** Everything that cannot change between two polls
  — the model, the static registers, which optional components exist — belongs
  to setup, so the polling path stays a fixed list of components to read.
