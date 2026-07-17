---
title: Library entrypoint
description: How the main entrypoint of a device library built on modbus-connection comes together — a top-level device object over Components and a ComponentGroup.
---

modbus-connection is a foundation you build a **device library** on. A good device
library exposes one **top-level object** that a consumer constructs from a
`ModbusUnit`, and reads sub-systems as plain Python attributes. This page shows
the shape, using the [`trovis-modbus`](https://github.com/Tom-Bom-badil/trovis-modbus)
library (a Samson TROVIS 557x heating controller) as the worked example.

## The shape

The entrypoint class:

1. takes a `ModbusUnit` — never a connection, and never a host/port. The consumer
   owns the connection and hands you a unit.
2. constructs its sub-systems as [`Component`](/modbus-connection/modelling/overview/)
   instances,
3. applies the device's readable ranges to them,
4. pools them into one [`ComponentGroup`](/modbus-connection/modelling/component-group/), and
5. exposes `async_update()` plus typed access to each sub-system.

```python
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from modbus_connection.model import Component, ComponentGroup

from .sensors import Sensors
from .controller import Controller
from .heating_circuit import HeatingCircuit
from .hot_water import HotWater
from .ranges import ranges_for_model, heating_circuit_count

if TYPE_CHECKING:
    from modbus_connection import ModbusUnit


class Trovis557x:
    """A Samson TROVIS 557x heating controller."""

    def __init__(
        self,
        unit: ModbusUnit,
        *,
        model: int = 5578,
        detected_sensors: Iterable[str] = (),
    ) -> None:
        self._unit = unit
        self.model = model
        self.detected_sensors = frozenset(detected_sensors)

        # Sub-systems, each a Component. Repeated ones take an index.
        self.controller = Controller(unit)
        self.sensors = Sensors(unit)
        self.heating_circuit_1 = HeatingCircuit(unit, index=1)
        self.heating_circuit_2 = HeatingCircuit(unit, index=2)
        self.hot_water = HotWater(unit)

        # Apply the model's readable ranges to every sub-system, so the group's
        # pooled reads never cross an unreadable gap.
        register_ranges, coil_ranges = ranges_for_model(model)
        for component in self.components:
            component.register_ranges = register_ranges
            component.coil_ranges = coil_ranges

        # One pooled reader for the whole device.
        self._group = ComponentGroup(unit, self.components)

    @property
    def components(self) -> tuple[Component, ...]:
        """Every actively polled sub-system."""
        return (
            self.controller,
            self.sensors,
            self.heating_circuit_1,
            self.heating_circuit_2,
            self.hot_water,
        )

    async def async_update(self) -> None:
        """Refresh all sub-systems in pooled Modbus reads."""
        await self._group.async_update()
```

The consumer then works entirely in Python objects:

```python
import asyncio
from modbus_connection import ModbusTcpParams
from modbus_connection.tmodbus import ModbusConnection
from trovis_modbus import Trovis557x


async def main() -> None:
    conn = ModbusConnection(
        ModbusTcpParams(host="192.168.1.50", port=502, framer="rtu")
    )
    device = Trovis557x(conn.for_unit(246))
    try:
        await device.async_update()  # first request connects
    finally:
        await conn.close()

    print("Outside temperature:", device.sensors.outside_1)
    print("Rk1 day setpoint:", device.heating_circuit_1.room_setpoint_day)


asyncio.run(main())
```

## A setup probe

A device whose layout depends on its model shouldn't read everything before it
knows the model. Expose a lightweight **classmethod probe** that reads only the
identity registers it needs to configure the full object:

```python
@dataclass(frozen=True)
class TrovisProbe:
    model: int
    detected_sensors: tuple[str, ...]


class Trovis557x:
    @classmethod
    async def async_probe(cls, unit: ModbusUnit) -> TrovisProbe:
        """Read only the safe identity + sensor data needed for setup."""
        model = (await unit.read_holding_registers(0, 1))[0]

        register_ranges, coil_ranges = ranges_for_model(model)
        sensors = Sensors(unit)
        sensors.register_ranges = register_ranges
        sensors.coil_ranges = coil_ranges
        await sensors.async_update()

        return TrovisProbe(model=model, detected_sensors=sensors.detected_sensor_names)
```

The consumer probes first, then constructs the full device from the result:

```python
probe = await Trovis557x.async_probe(unit)
device = Trovis557x(unit, model=probe.model, detected_sensors=probe.detected_sensors)
await device.async_update()
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
  backend-neutral — it works over pymodbus, tmodbus, or the mock unchanged.
- **One sub-system per `Component`.** Group registers by function; give each its
  own file. It keeps the address map readable and lets a sub-system refresh alone.
- **Pool with a `ComponentGroup`.** The whole device reads in a handful of Modbus
  calls instead of one per field.
- **Carry metadata on the fields.** `unit=`, ranges, and validators live next to
  the address, so the model *is* the datasheet.

## Next

- [Query helper](/modbus-connection/patterns/query-helper/) — a standalone CLI to
  dump every value from a real device without any application around it.
