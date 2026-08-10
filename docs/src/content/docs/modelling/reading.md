---
title: Reading a device
description: How a component turns its fields into as few Modbus reads as possible — block pooling, readable address ranges, a refused block, and the raw register map.
---

`async_update()` never issues one Modbus request per field. The planner turns a
component's declared layout into a handful of block reads, and this page covers
what shapes them: the two pooling knobs, the device's readable ranges, what
happens when the device refuses a block, and how to get the raw words back.

## Reads are pooled into blocks

The planner merges addresses that are close together into a single block read
rather than issuing one Modbus request per field. Two knobs tune this, settable
as `Component` class attributes:

- **`max_gap`** (default `16`) — in gap-based planning, fields within this many
  addresses share one read. Higher means fewer requests but more over-reading;
  lower is safer for devices that reject reads of unmapped registers.
- **`max_span`** (default `125`, the Modbus per-request ceiling) — the widest a
  single block read may be. Lower it for a gateway that caps reads shorter.

In a [`ComponentGroup`](/modbus-connection/modelling/component-group/) the same
merging runs across the members, but only where their addresses meet: the space
between two components belongs to neither, so no block covers it.

The read plan is derived from the static field layout and **cached on the first
`async_update`**. The fields and ranges are read once then; to change the layout,
build a new component.

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
[`base_offset`](/modbus-connection/modelling/placement/#base_offset--the-whole-layout-at-another-address)
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

A per-field `stride` is the exception: an `index` shifts each field on its own
rather than the whole block, so state an indexed layout's ranges at the addresses
it actually reads.

## When a block read fails

An `async_update()` either applies fully or raises — it never applies a block read
part-way. If the device answers one of the block reads with a Modbus **exception
response**, the update raises the
[typed exception](/modbus-connection/connection/reference/#modbusexceptionerror)
for that code — the *why* — with the refused block on `.block` — the *where*:

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
component so a missing one doesn't fail the rest of the update. The same applies to
a [`ComponentGroup`](/modbus-connection/modelling/component-group/): any block
across its pooled members failing fails the whole group's update.

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
