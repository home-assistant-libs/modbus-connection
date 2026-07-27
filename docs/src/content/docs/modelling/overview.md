---
title: Device modelling overview
description: Map a device's registers and coils to typed attributes with Component, and read it in as few Modbus calls as possible.
---

`modbus_connection.model` is an optional, backend-neutral framework for mapping a
device's registers and coils to typed Python attributes, then reading the whole
device — or one sub-system — in as few Modbus calls as possible. It talks only to
a `ModbusUnit`, so it runs over any backend (or the [mock](/modbus-connection/reference/testing/)).

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

## Reads are pooled into blocks

The planner merges addresses that are close together into a single block read
rather than issuing one Modbus request per field. Two knobs tune this, settable
as `Component` class attributes:

- **`max_gap`** (default `16`) — in gap-based planning, fields within this many
  addresses share one read. Higher means fewer requests but more over-reading;
  lower is safer for devices that reject reads of unmapped registers.
- **`max_span`** (default `125`, the Modbus per-request ceiling) — the widest a
  single block read may be. Lower it for a gateway that caps reads shorter.

The read plan is derived from the static field layout and **cached on the first
`async_update`**. The fields and ranges are read once then; to change the layout,
build a new component.

## When a block read fails

An `async_update()` either applies fully or raises — it never applies a block read
part-way. If the device answers one of the block reads with a Modbus **exception
response** (an illegal data address, say, because a block it used to serve stopped
answering), the update raises
[`BlockReadError`](/modbus-connection/reference/exceptions/#blockreaderror). It is a
[`ModbusExceptionError`](/modbus-connection/reference/exceptions/#modbusexceptionerror),
so `.exception_code` says *why* the read was refused, plus `.space`, `.address`, and
`.count` for *which* block:

```python
from modbus_connection import BlockReadError

try:
    await meter.async_update()
except BlockReadError as err:
    log.warning(
        "block read failed at %s %d: code %s",
        err.space,
        err.address,
        err.exception_code,
    )
```

If some blocks are legitimately optional on a device, read them on a separate
component so a missing one doesn't fail the rest of the update. The same applies to
a [`ComponentGroup`](/modbus-connection/modelling/component-group/): any block
across its pooled members failing raises `BlockReadError` for the whole group.

## Readable address ranges

Many devices only answer reads inside specific ranges, and a read that crosses a
gap is rejected. Declare the device's readable ranges and the planner merges
**only within a range**, never across a boundary:

```python
class Thermostat(Component):
    # (low, high) inclusive. The device answers 0–6 and 9–40 but nothing in
    # between, so 7–8 are never read and a 0..40 block is split at the gap.
    register_ranges = ((0, 6), (9, 40))
    coil_ranges = ((0, 15),)

    model = integer(0)
    outside = gauge(9, 0.1, unit="°C")
```

With `register_ranges` declared, `max_gap` is ignored. Leave the ranges as the
default `None` for a device with a contiguous map (plain gap-based planning).

Ranges are part of the **declared layout**, so they are written in the same
coordinates as the field addresses beside them and move with the component:
placing the layout somewhere else with
[`base_offset`](/modbus-connection/modelling/repeats/#base_offset--the-whole-layout-at-another-address)
shifts the ranges by the same amount. Declaring a layout relative to its block
start therefore keeps working when the block sits elsewhere on the device:

```python
class Boiler(Component):
    register_ranges = ((0, 5), (50, 50))  # relative to the block start
    state = integer(1)
    target = gauge(50, 0.1)


# The ranges resolve to (2000, 2005) and (2050, 2050) along with the fields.
boiler = Boiler(unit, base_offset=2000)
```

A component at the default `base_offset=0` is unaffected — declared and resolved
addresses are the same — so a device-wide map applied to unplaced components stays
correct. A layout addressed by `index` and a per-field `stride` is the exception:
that shift is per field rather than the whole block, so state the ranges of an
indexed layout at the addresses it actually reads.

## Firmware-dependent register subsets

Some devices serve only a **subset** of a known layout, and which registers they
answer depends on the firmware — with no model or version register to key off. The
layout is otherwise perfectly typed; only the served subset varies per device. A
block read is atomic, so a single unserved register *inside* a block fails the whole
read (see [above](#when-a-block-read-fails)) — and separating blocks onto their own
components doesn't help when the served and unserved registers are interleaved.

`Component.restrict_fields(names)` narrows a component to the fields the device
actually serves. Probe the device once at setup to learn which fields answer, then
keep those:

```python
boiler = Boiler(unit, base_offset=2000)
boiler.restrict_fields(served_field_names)  # keep just these
await boiler.async_update()  # stock update, reads only served registers
```

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

`Component.write(field, value)` writes a writable register or coil by attribute
name. For registers it picks the function code by payload width — FC06
(write-single-register) for a one-word value, FC16 (write-multiple-registers)
otherwise. Some devices honour only FC16 even for a single register; pass
`force_fc16=True` on the field:

```python
from modbus_connection.model import Component, integer


class Inverter(Component):
    limit = integer(0, writable=True, force_fc16=True)
```

A field must be marked `writable` to be written. Passing a **validator** callable
instead of `writable=True` both marks the field writable and vets the value
before each write — it is called with the requested value and returns the value
to actually write (vetted or coerced), or raises to reject it, before anything
reaches the device:

```python
def in_range(value: int) -> int:
    if not 0 <= value <= 100:
        raise ValueError(f"{value} out of range")
    return value


class Boiler(Component):
    setpoint = integer(0, writable=in_range)
```

The library ships no validators of its own; for ready-made ones reach for
[probatio](https://github.com/frenck/probatio). Override `write()` in a subclass
for any device-specific write sequencing.

## Listeners

Each component has its own update listeners, fired after each update:

```python
unsubscribe = meter.add_update_listener(lambda: print("updated", meter.voltage))
await meter.async_update()  # prints
unsubscribe()
```

## Raw diagnostics

Alongside the decoded read, `async_read_raw()` returns the device's **raw**
register map — for a diagnostics download, or to debug a register layout. It runs
the same reads as `async_update()` (and, like it, refreshes the fields and fires
listeners), but additionally hands back the raw words and bits keyed by absolute
address:

```python
raw = await meter.async_read_raw()
# {"holding": {0: 2301, 1: 47, ...}, "coil": {0: True}}
```

The result is keyed by the four Modbus spaces — `holding`, `input`, `coil`,
`discrete` — each an address-keyed map, addresses ascending. `ComponentGroup` and
`ManualComponent` expose the same method (a group's is merged across its members);
see [Diagnostics](/modbus-connection/home-assistant/integration/#diagnostics) for
the Home Assistant download handler.

## Beyond the built-ins

Shaping that the field types don't cover — composing or transforming a value,
packed dates/times — is left to you via a private field plus a `@property`, so
static typing stays exact:

```python
from modbus_connection.model import Component, string


class Controller(Component):
    _firmware = string(10, 4)  # 4 registers of ASCII, e.g. "1.23"

    @property
    def model(self) -> str | None:
        firmware = self._firmware
        return f"TROVIS 5576 ({firmware})" if firmware is not None else None
```

## Where to next

- [Built-in fields](/modbus-connection/modelling/fields/) — every generic field type.
- [SunSpec](/modbus-connection/modelling/sunspec/) — the SunSpec point types.
- [Repeated sub-units](/modbus-connection/modelling/repeats/) — `stride` / `index`
  and the runtime-counted `repeating_group`.
- [Component groups](/modbus-connection/modelling/component-group/) — refresh
  several components in one pooled read.
- [Manual components](/modbus-connection/modelling/manual-component/) — build the
  layout at runtime from config.
