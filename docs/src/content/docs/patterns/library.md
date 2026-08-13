---
title: The device object
description: How the top-level device object of a library built on modbus-connection comes together — one typed class over Components and a ComponentGroup.
---

modbus-connection is a foundation you build a **device library** on. A good device
library exposes one **top-level object** that a consumer constructs from a
`ModbusUnit`, and reads sub-systems as plain Python attributes. This page shows
the shape, using the [`trovis-modbus`](https://github.com/Tom-Bom-badil/trovis-modbus)
library (a Samson TROVIS 557x heating controller) as the worked example.

## The shape

The device object:

1. takes a `ModbusUnit` — never a connection, and never a host/port. The consumer
   owns the connection and hands you a unit.
2. constructs its sub-systems as [`Component`](/modbus-connection/modelling/overview/)
   instances,
3. sets itself up once — reading everything that never changes and settling
   which components this device serves — from the first `async_update()`,
4. pools the ones it polls into one [`ComponentGroup`](/modbus-connection/modelling/component-group/), and
5. exposes `async_update()` plus typed access to each sub-system.

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from modbus_connection import IllegalDataAddressError, IllegalFunctionError
from modbus_connection.model import Component, ComponentGroup

from .sensors import Sensors
from .controller import Controller
from .heating_circuit import HeatingCircuit
from .hot_water import HotWater

if TYPE_CHECKING:
    from modbus_connection import ModbusUnit


async def _optional[C: Component](component: C) -> C | None:
    """Read an optional sub-system; None if this device does not have it."""
    try:
        await component.async_update()
    except (IllegalDataAddressError, IllegalFunctionError):
        return None
    return component


class Trovis557x:
    """A Samson TROVIS 557x heating controller."""

    def __init__(self, unit: ModbusUnit) -> None:
        self._unit = unit

        # Sub-systems, each a Component. Repeated ones take an index.
        self.controller = Controller(unit)
        self.sensors = Sensors(unit)
        self.heating_circuit_1 = HeatingCircuit(unit, index=1)
        # Optional: filled in by the first update if this model has them.
        self.heating_circuit_2: HeatingCircuit | None = None
        self.hot_water: HotWater | None = None

        self._group: ComponentGroup | None = None

    async def _async_setup(self) -> None:
        """Read what never changes, and find which sub-systems this model has.

        Runs from the first ``async_update()``, and again on the next one if
        the device was unreachable.
        """
        await self.controller.async_update()  # identity: read once, never polled

        heating_circuit_2 = await _optional(HeatingCircuit(self._unit, index=2))
        hot_water = await _optional(HotWater(self._unit))

        self.heating_circuit_2 = heating_circuit_2
        self.hot_water = hot_water
        self._group = ComponentGroup(
            self._unit,
            [
                c
                for c in (
                    self.sensors,
                    self.heating_circuit_1,
                    heating_circuit_2,
                    hot_water,
                )
                if c is not None
            ],
        )

    async def async_update(self) -> None:
        """Refresh all polled sub-systems; the first call sets the device up."""
        if self._group is None:
            await self._async_setup()
        assert self._group is not None  # _async_setup() always builds it
        await self._group.async_update()
```

The consumer then works entirely in Python objects:

```python
import asyncio
from modbus_connection import ModbusTcpParams
from modbus_connection.tmodbus import ModbusConnection
from trovis_modbus import Trovis557x


async def main() -> None:
    connection = ModbusConnection(
        ModbusTcpParams(host="192.168.1.50", port=502, framer="rtu")
    )
    try:
        unit = connection.for_unit(246)
        device = Trovis557x(unit)
        await device.async_update()

        print("Outside temperature:", device.sensors.outside_1)
        print("Rk1 day setpoint:", device.heating_circuit_1.room_setpoint_day)
        if device.hot_water is not None:  # absent on some models
            print("Hot water:", device.hot_water.temperature)
    finally:
        await connection.close()


asyncio.run(main())
```

## Resilient polling

One `ComponentGroup` over the whole device is the simplest polling path, and it is
fine for a handful of sub-systems. But a group's read plan is
[all-or-nothing](/modbus-connection/modelling/reading/#when-a-block-read-fails):
one refused — or merely slow — block anywhere in it fails the entire update. On a
real inverter that is one 48-register block timing out and all 88 values going
unavailable together.

So once a device is big enough for that to hurt, treat **each component as a
failure domain** and poll them one at a time. You give up pooling *across*
components — each still pools its own fields — and get an update where one sick
sub-system costs you that sub-system only:

```python
from __future__ import annotations

from dataclasses import dataclass, field

from modbus_connection import ModbusConnectionError, ModbusError

# Every component this device polls, in read order.
_POLLED = ("sensors", "heating_circuit_1", "heating_circuit_2", "hot_water")


@dataclass
class UpdateReport:
    """What one poll managed to refresh."""

    updated: list[str] = field(default_factory=list)
    failed: dict[str, ModbusError] = field(default_factory=dict)


class Trovis557x:
    # __init__ and _async_setup() as above, except that setup ends with
    #   self._polled = tuple(n for n in _POLLED if getattr(self, n) is not None)
    # — the poll list filtered to what this model actually has, which doubles
    # as the "set up" marker in place of the group.

    async def async_update(self) -> UpdateReport:
        """Refresh every polled sub-system; the first call sets the device up."""
        if self._polled is None:
            await self._async_setup()
        assert self._polled is not None  # _async_setup() always builds it

        report = UpdateReport()
        for name in self._polled:
            component = getattr(self, name)
            try:
                await component.async_update(notify=False)
            except ModbusConnectionError:
                raise  # the link is down; the rest would only wait for timeouts
            except ModbusError as err:
                report.failed[name] = err
            else:
                report.updated.append(name)

        for name in report.updated:  # nothing fires until the cycle is done
            getattr(self, name).notify()
        return report
```

- **Contain a component, abort on the link.** `ModbusTimeoutError` is a
  [sibling](/modbus-connection/connection/reference/#modbustimeouterror) of
  `ModbusConnectionError`, not a subclass, so the order above is exactly right: a
  slow block stays contained while a dead link aborts the cycle.
- **Notify at the end, and only the components that refreshed.** With
  `notify=False` on each read, no listener sees a half-updated device.
- **Report, don't raise.** A failed component keeps its previous values — the
  store is only written when a component reads fully — so the report is what tells
  the consumer which values are stale, and it decides what that means. Only a dead
  link raises.
- **Let a failed setup retry.** Because `self._polled` is the setup marker, a
  device that was unreachable during setup simply sets up on the next poll.

Setup reads follow the same rule. `_optional()` must treat only
`IllegalDataAddressError` and `IllegalFunctionError` as "this device does not have
it" — anything else is a bad moment, not a missing sub-system, and latching
absence from it silently drops a component for the lifetime of the object.

## Principles

- **Take a `ModbusUnit`, not a connection.** The consumer owns and closes the
  link; your library only reads and writes registers. This keeps the library
  backend-neutral — it works over tmodbus, pymodbus, or the mock unchanged.
- **One sub-system per `Component`.** Group registers by function; give each its
  own file. It keeps the address map readable and lets a sub-system refresh alone.
- **Pool with a `ComponentGroup`.** The whole device reads in a handful of Modbus
  calls instead of one per field.
- **Carry metadata on the fields.** `unit=`, ranges, and validators live next to
  the address, so the model *is* the datasheet.
- **Decide once, poll forever.** Everything that cannot change between two polls
  — the model, the static registers, which optional components exist — belongs
  to setup, so the polling path stays a fixed list of components to read.
- **A component is a failure domain.** Poll them one by one and report what
  failed; the device stays as available as the device actually is.
