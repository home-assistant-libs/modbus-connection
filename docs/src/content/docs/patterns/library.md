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
3. exposes `async_setup()`, which reads everything that never changes and settles
   which components this device serves,
4. pools the ones it polls into one [`ComponentGroup`](/modbus-connection/modelling/component-group/), and
5. exposes `async_update()` plus typed access to each sub-system.

A component some firmware revisions do not serve is probed at setup, and only
the ones that answered are pooled. Probing costs nothing: the read that
decides whether a component exists is the read that fills it. Catch only the
refusal that means *absent* — usually `IllegalDataAddressError`; a busy or
failing device is transient, and treating that as absent silently drops
registers a healthy device serves.

Deciding membership at setup is also what makes an optional component free to
poll. Probe *during* polling instead and it can never join the pooled read —
one refusal would fail the whole group and take the required values with it —
so it costs an extra round trip forever.

```python
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from modbus_connection import IllegalDataAddressError
from modbus_connection.model import Component, ComponentGroup

from .sensors import Sensors
from .controller import Controller
from .heating_circuit import HeatingCircuit
from .hot_water import HotWater

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

        # Both built by async_setup, once it knows what this device serves.
        self._polled: tuple[Component, ...] = ()
        self._group: ComponentGroup | None = None

    async def async_setup(self) -> None:
        """Read what never changes, then pool what this device actually polls."""
        await self.controller.async_update()  # identity: read once, never polled

        polled = [self.sensors, self.heating_circuit_1]
        for component in (self.heating_circuit_2, self.hot_water):  # not on every model
            try:
                await component.async_update()
            except IllegalDataAddressError:
                continue  # this firmware does not serve it
            polled.append(component)

        self._polled = tuple(polled)
        self._group = ComponentGroup(self._unit, self._polled)

    @property
    def components(self) -> tuple[Component, ...]:
        """Every actively polled sub-system."""
        return self._polled

    async def async_update(self) -> None:
        """Refresh all polled sub-systems in pooled Modbus reads."""
        assert self._group is not None, "async_setup() must run first"
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
        await device.async_setup()  # once
        await device.async_update()  # every interval

        print("Outside temperature:", device.sensors.outside_1)
        print("Rk1 day setpoint:", device.heating_circuit_1.room_setpoint_day)
    finally:
        await connection.close()


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

        sensors = Sensors(unit)
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
