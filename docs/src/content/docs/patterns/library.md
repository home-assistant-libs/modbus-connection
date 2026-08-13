---
title: The device object
description: How the top-level device object of a library built on modbus-connection comes together — one typed class over Components, polled so a failure stays local.
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
4. polls each of them on its own, so one that fails does not take the others
   down with it, and
5. exposes `async_update()` plus typed access to each sub-system.

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from modbus_connection import (
    IllegalDataAddressError,
    IllegalFunctionError,
    ModbusConnectionError,
    ModbusError,
)
from modbus_connection.model import Component

from .sensors import Sensors
from .controller import Controller
from .heating_circuit import HeatingCircuit
from .hot_water import HotWater

if TYPE_CHECKING:
    from modbus_connection import ModbusUnit

# Every sub-system that can be polled, in read order.
_POLLED = ("sensors", "heating_circuit_1", "heating_circuit_2", "hot_water")


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

        # _POLLED filtered to what this model has, None until setup ran — so it
        # doubles as the setup marker.
        self._polled: tuple[str, ...] | None = None

    async def _async_setup(self) -> None:
        """Read what never changes, and settle which sub-systems this model has.

        Runs from the first ``async_update()``, and again on the next one if
        the device was unreachable.
        """
        await self.controller.async_update()  # identity: read once, never polled

        self.heating_circuit_2 = await _optional(HeatingCircuit(self._unit, index=2))
        self.hot_water = await _optional(HotWater(self._unit))

        self._polled = tuple(n for n in _POLLED if getattr(self, n) is not None)

    async def async_update(self) -> UpdateReport:
        """Refresh every polled sub-system; the first call sets the device up."""
        if self._polled is None:
            await self._async_setup()
        assert self._polled is not None  # _async_setup() always builds it

        report = UpdateReport()
        for name in self._polled:
            try:
                await getattr(self, name).async_update(notify=False)
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

Each component is its own failure domain: a bad block costs that sub-system and
nothing else. You give up pooling *across* components — each still pools its own
fields — which in practice usually costs no extra reads at all, since sub-systems
tend to own separate stretches of the register map.

- `ModbusTimeoutError` is a
  [sibling](/modbus-connection/connection/reference/#modbustimeouterror) of
  `ModbusConnectionError`, not a subclass — hence the order of those two
  `except` clauses.
- `notify=False` plus a notify pass at the end keeps listeners from seeing a
  half-updated device.
- A failed component keeps its previous values, so the report is what tells the
  consumer which ones are stale.
- `self._polled` is the setup marker: a device unreachable during setup sets up
  on the next poll.
- Only `IllegalDataAddressError` and `IllegalFunctionError` mean "this device
  does not have it". Latching absence from anything else drops a sub-system for
  the lifetime of the object.

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
- **A component is a failure domain.** Poll them one by one and report what
  failed; the device stays as available as the device actually is. Each still
  pools its own fields, so this is a handful of Modbus calls, not one per field.
