---
title: Reading a device
description: How a component turns its fields into as few Modbus reads as possible — block pooling, readable address ranges, a refused block, and the raw register map.
---

`async_update()` never issues one Modbus request per field. The planner turns a
component's declared layout into a handful of block reads. This page covers
what shapes those reads: the two pooling knobs, the device map you can declare
on top of them, what happens when the device refuses a block, and how to get
the raw words back.

## Reads are pooled into blocks

The planner merges addresses that are close together into a single block read.
Two knobs tune this, settable as `Component` class attributes:

- **`max_gap`** (default `16`) — in gap-based planning, fields within this many
  addresses share one read. Higher means fewer requests but more over-reading.
  Lower is safer for devices that reject reads of unmapped registers.
- **`max_span`** (default `125`, the Modbus per-request ceiling) — the widest a
  single block read may be. Lower it for a gateway that caps reads shorter.

A block covers the registers between the fields it merges. A device is normally
happy to serve those. Where it is not, or where the blocks should be wider than
`max_gap` allows, declare the device's map — see below.

In a [`ComponentGroup`](/modbus-connection/modelling/component-group/) the same
merging runs across the members, but never across the space between two of them.

The read plan is derived from the static field layout and **cached on the first
`async_update`**. The fields and ranges are read once at that point. To change
the layout, build a new component.

## Readable address ranges

`register_ranges` states which addresses the **device** answers — its map, not
your layout's. **Most libraries never declare one**: gap planning already reads
a device that serves anything inside its documented blocks. Declare a map when
the device is stricter than that, or when you want fewer round trips. A map
does two things `max_gap` cannot:

- **It allows a wider read.** Inside a range the planner merges freely, up to
  `max_span`, over registers no field claims. That gives one read of a whole
  block instead of one per cluster of fields. `max_gap` no longer applies where
  a map does.
- **It forbids a merge.** A block never crosses a range boundary, however small
  the gap. This is the only way to keep reads off registers the device refuses.
  The default `max_gap` of 16 will bridge a two-register hole otherwise.

```python
class Thermostat(Component):
    # (low, high) inclusive. The device answers 0–6 and 9–40 but nothing in
    # between: 7–8 are never read, and everything from 9 to 40 may share one.
    register_ranges = ((0, 6), (9, 40))
    coil_ranges = ((0, 15),)

    model = integer(0)
    outside = gauge(9, 0.1, unit="°C")
```

Each space has its own map: `register_ranges` for the component's register
space, `coil_ranges` for coils, `discrete_ranges` for discrete inputs. Each is
independent — a map for one space says nothing about another.

Ranges are part of the **declared layout**. They are written in the same
coordinates as the field addresses beside them and move with the component:
placing the layout somewhere else with
[`base_offset`](/modbus-connection/modelling/placement/#base_offset--the-whole-layout-at-another-address)
shifts the ranges by the same amount. A layout declared relative to its block
start therefore keeps working when the block sits elsewhere on the device:

```python
class Boiler(Component):
    register_ranges = ((0, 5), (50, 50))  # relative to the block start
    state = integer(1)
    target = gauge(50, 0.1)


# The ranges resolve to (2000, 2005) and (2050, 2050) along with the fields.
boiler = Boiler(unit, base_offset=2000)
```

A per-field `stride` is the exception: an `index` shifts each field on its own
rather than the whole block. State an indexed layout's ranges at the addresses
it actually reads.

## When a block read fails

An `async_update()` either applies fully or raises. It never applies a block
read part-way. If the device answers one of the block reads with a Modbus
**exception response**, the update raises the
[typed exception](/modbus-connection/connection/reference/#modbusexceptionerror)
for that code, with the refused block on `.block`:

```python
from modbus_connection import IllegalDataAddressError, ModbusExceptionError

try:
    await meter.async_update()
except IllegalDataAddressError as err:
    ...  # e.g. this firmware does not serve the component
except ModbusExceptionError as err:
    log.warning("block read failed at %s: code %s", err.block, err.exception_code)
```

If some blocks are legitimately optional on a device, read them on a separate
component. A missing one then does not fail the rest of the update. The same
applies to a [`ComponentGroup`](/modbus-connection/modelling/component-group/):
any failing block across its pooled members fails the whole group's update.

## Raw diagnostics

`async_read_raw()` returns the device's **raw** register map alongside the
decoded read — for a diagnostics download, or to debug a register layout. It
runs the same reads as `async_update()`, and like it refreshes the fields and
fires listeners. In addition it returns the raw words and bits keyed by
absolute address:

```python
raw = await meter.async_read_raw()
# {"holding": {0: 2301, 1: 47, ...}, "coil": {0: True}}
```

The result is keyed by the four Modbus spaces — `holding`, `input`, `coil`,
`discrete` — each an address-keyed map, addresses ascending. `ComponentGroup`
and `ManualComponent` expose the same method (a group's is merged across its
members). See
[Diagnostics](/modbus-connection/home-assistant/integration/#diagnostics) for
the Home Assistant download handler.
