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
3. pools them into one [`ComponentGroup`](/modbus-connection/modelling/component-group/), and
4. exposes `async_update()` plus typed access to each sub-system.

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
    connection = ModbusConnection(
        ModbusTcpParams(host="192.168.1.50", port=502, framer="rtu")
    )
    try:
        unit = connection.for_unit(246)
        device = Trovis557x(unit)
        await device.async_update()

        print("Outside temperature:", device.sensors.outside_1)
        print("Rk1 day setpoint:", device.heating_circuit_1.room_setpoint_day)
    finally:
        await connection.close()


asyncio.run(main())
```

## Two phases: setup and polling

A device library does two different jobs, and keeping them apart removes most of
the branching people end up writing:

**Setup runs once.** Read the static registers — serial number, model, firmware
and hardware versions — and probe whichever components some firmware revisions
do not serve. Record what answered.

**Polling runs every interval.** One call over a fixed set of components. No
capability checks, no "have I read this yet" flags, no branches.

The line matters because a structural refusal is a property of the firmware, not
of the moment: a device that refuses a block on this poll refuses it on every
poll. Asking once and remembering the answer is both cheaper and simpler than
tolerating the refusal forever.

```python
class Device:
    async def async_setup(self) -> None:
        """Runs once: read what never changes, and find out what exists."""
        await self.info.async_update()  # static: read here, never polled

        polled = [self.realtime]
        for component in (self.battery, self.meter):  # optional on some firmware
            try:
                await component.async_update()
            except (IllegalFunctionError, IllegalDataAddressError):
                continue  # this firmware does not serve it
            polled.append(component)

        self._group = ComponentGroup(self._unit, polled)

    async def async_update(self) -> None:
        """Runs every interval."""
        await self._group.async_update()
```

Two consequences worth stating:

- **Static components stay out of the polled group.** They belong to setup. A
  separate component that setup reads and polling never touches needs no flag.
- **Probing a component is free.** The read that decides whether it exists is
  the same read that fills it, so a component that answers is already populated
  when setup finishes.

Discovering membership at setup is also why an optional component costs nothing
per poll. If you instead probe *during* polling, the optional components can
never join the pooled read — one refusal would fail the whole group and take the
required values with it — so each costs an extra round trip forever.

### Which refusal means "not served"

The device answers a [typed exception](/modbus-connection/connection/reference/#modbusexceptionerror);
which ones mean the registers are absent is protocol semantics, not device
policy:

| Refusal | Meaning | At setup |
| --- | --- | --- |
| `IllegalFunctionError` (1) | The device does not implement the function code. | Not served — drop it. |
| `IllegalDataAddressError` (2) | The device does not serve the address. | Not served — drop it. |
| `IllegalDataValueError` (3) | The request itself was wrong — usually a quantity the device rejects. | Propagate: a bug in the layout. |
| `ServerDeviceFailureError` (4), `AcknowledgeError` (5), `ServerDeviceBusyError` (6) | The registers exist; the read went wrong or the device is busy. | Propagate: transient, retry later. |
| Gateway codes (10, 11) | The gateway could not reach the device. | Propagate: nothing was learned about the map. |

Only codes 1 and 2 say something structural. Treating a transient failure as
"absent" silently drops registers a healthy device serves — so catch those two,
and let everything else fail setup.

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
