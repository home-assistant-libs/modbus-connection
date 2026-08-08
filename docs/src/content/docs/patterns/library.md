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

from modbus_connection import IllegalDataAddressError
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
    except IllegalDataAddressError:
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
        the device was unreachable — which is why nothing is kept until every
        probe has answered. Private: a second run after a successful one would
        rebuild the sub-systems and discard everything polled into them.
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

## Writes behind a switch

Writing to industrial devices is often gated. A common pattern is a global
"writing enabled" switch the consumer flips explicitly, so a write can never
happen by accident. Many devices back this with a **lock register** — write `1`
to unlock writes, `0` to lock them again — so the switch is just a register write:

```python
# The device's write-enable register (1 = unlocked, 0 = locked).
_WRITE_LOCK_ADDRESS = 100


class Trovis557x:
    async def async_enable_writing(self) -> None:
        await self._unit.write_register(_WRITE_LOCK_ADDRESS, 1)
        self._writing_enabled = True

    async def async_disable_writing(self) -> None:
        await self._unit.write_register(_WRITE_LOCK_ADDRESS, 0)
        self._writing_enabled = False
```

Some devices instead expect an access code rather than a plain `1`; write that
value to the same register. Either way it's an ordinary Modbus write — no special
helper needed.

```python
await device.async_enable_writing()
try:
    await device.heating_circuit_1.write("room_setpoint_day", 21.5)
finally:
    await device.async_disable_writing()
```

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
  to setup, so the polling path stays a single call over a fixed group.
