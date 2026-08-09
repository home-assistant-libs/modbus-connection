---
title: Device modelling overview
description: Map a device's registers and coils to typed attributes with Component, and read it in as few Modbus calls as possible.
---

`modbus_connection.model` is an optional, backend-neutral framework for mapping a
device's registers and coils to typed Python attributes, then reading the whole
device — or one sub-system — in as few Modbus calls as possible. It talks only to
a `ModbusUnit`, so it runs over any backend (or the [mock](/modbus-connection/patterns/testing/)).

## A first component

A `Component` is a device sub-system. Declare its registers and coils as class
attributes using the [built-in fields](/modbus-connection/modelling/fields/):

```python
from modbus_connection.model import Component, gauge, uint32, coil


class Meter(Component):
    voltage = gauge(0, 0.1, unit="V")  # scaled 16-bit
    """Grid voltage."""

    current = gauge(1, 0.1, unit="A")
    """Grid current."""

    energy = uint32(2, unit="Wh")  # 32-bit over two registers
    """Lifetime energy."""

    relay = coil(0, writable=True)
    """Load relay."""


meter = Meter(unit)
await meter.async_update()  # one block read
meter.voltage  # float | None
await meter.write("relay", True)
```

The string under each field is an attribute docstring — optional, but editors
show it when hovering `meter.voltage` anywhere in the codebase.

`async_update()` reads every field, decodes it, and stores the result. Reading an
attribute returns the decoded value or `None` (a field that hasn't been read yet,
or a device sentinel that decodes to "no value"). Because a component reads only
its own registers, it can refresh independently.

The update is not one request per field: neighbouring addresses are pooled into
block reads, bounded by what the device is willing to serve. See
[Reading a device](/modbus-connection/modelling/reading/) for the pooling knobs,
the readable ranges, and what a refused block does to an update.

## Register spaces: holding vs input

A component's register fields default to the **holding** space (FC03). For a
read-only sub-system whose data lives in **input** registers (FC04), set
`register_space = "input"` — the field declarations are unchanged:

```python
class Sensors(Component):
    register_space = "input"
    flow_temp = gauge(5, 0.1, unit="°C")  # read with FC04
```

Input and holding are separate address spaces (input 507 ≠ holding 507), so the
planner never merges them into one read. Input registers are physically
read-only, so writing an `"input"` field raises.

## Bit spaces: coils vs discrete inputs

Bits work the same way over their own pair of spaces:

```python
from modbus_connection.model import Component, coil, discrete_input


class IO(Component):
    relay = coil(0, writable=True)  # FC01, read/write
    fault = discrete_input(0)  # FC02, read-only — distinct from coil 0
```

`coil` fields are read/written via FC01; `discrete_input` fields are read from
FC02 (read-only). A single component may declare both — coil 12 ≠ discrete input
12, so they are planned and read separately. `coil_ranges` constrains coils and
`discrete_ranges` constrains discrete inputs.

## Writing

`Component.write(field, value)` writes a register or coil by attribute name:

```python
await meter.write("relay", True)
```

The field must be marked
[`writable`](/modbus-connection/modelling/fields/#writable-fields-and-validators)
— optionally with a validator that vets the value before it reaches the device.
Override `write()` in a subclass for any device-specific write sequencing.

## Listeners

Each component has its own update listeners, fired after each update:

```python
unsubscribe = meter.add_update_listener(lambda: print("updated", meter.voltage))
await meter.async_update()  # prints
unsubscribe()
```

Pass `async_update(notify=False)` to read without firing the listeners, for a
caller that notifies them itself.

## Where to next

- [Built-in fields](/modbus-connection/modelling/fields/) — every generic field type.
- [Reading a device](/modbus-connection/modelling/reading/) — block pooling,
  readable ranges, failed blocks, and the raw register map.
- [Repeated sub-units](/modbus-connection/modelling/repeats/) — `stride` / `index`
  and the runtime-counted `repeating_group`.
- [Restricting fields](/modbus-connection/modelling/restricting-fields/) — narrow a
  component to the subset of a layout a device actually serves.
- [Component groups](/modbus-connection/modelling/component-group/) — refresh
  several components in one pooled read.
- [Manual components](/modbus-connection/modelling/manual-component/) — build the
  layout at runtime from config.
- [SunSpec](/modbus-connection/modelling/sunspec/) — the SunSpec point types.
- [Field reference](/modbus-connection/modelling/fields-reference/) and
  [Component reference](/modbus-connection/modelling/components-reference/) —
  every class, method, and field of the modelling layer.
