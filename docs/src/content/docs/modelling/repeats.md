---
title: Repeating groups
description: Model one sub-unit as a Component and let the parent size the list, from a fixed count or one the device reports at poll time.
---

Devices that expose several identical sub-units — heating circuits, channels,
phases, MPPT modules — repeat the same registers at a fixed step. A
`repeating_group` models one instance as a `Component` and hands the parent a
typed `list` of them. The count is a fixed `int`, or a register the device
reports at poll time: a SunSpec multiple-MPPT model (160) carries an `N` point
saying how many modules follow.

```python
from modbus_connection.model import Component, integer, repeating_group
from modbus_connection.model.sunspec import uint16


class MPPTModule(Component):  # one module, at instance 0's addresses
    dc_w = integer(11, scale_register=2)
    dc_v = integer(10, scale_register=1)


class Inverter(Component):
    modules = repeating_group(uint16(8), MPPTModule, stride=20)  # N at register 8


inv = Inverter(unit)
await inv.async_update()
inv.modules  # list[MPPTModule]
inv.modules[0].dc_w  # typed per-instance access
await inv.modules[2].write("dc_w", ...)  # writes go through the instance
```

For a layout a group can't express — a sub-unit whose registers are interleaved
by type across the map — place the instances by hand with
[`index` and a per-field `stride`](/modbus-connection/modelling/placement/).

## How the count is read

`count` is a `RegisterField` (read each poll) or a fixed `int`. Instance *i* has
every address of its declared layout shifted by `i * stride` on top of the
parent's own placement, so **`stride` is the block length**.

- A **fixed `int`** count is static, so its instances fold into the component's
  normal read — no extra pass.
- A **`RegisterField`** count needs a second pass: the count is read first, then
  the sized-out instances (pooled among themselves), since the count must be known
  before the instances it sizes can be planned. A float-typed count field (a
  SunSpec `uint16` `N` point) is accepted; the decoded count is truncated.

An unimplemented or unreadable count yields no instances. A component with a
`repeating_group` can refresh on its own or be pooled in a
[`ComponentGroup`](/modbus-connection/modelling/component-group/) — the group
reads the counts in its pooled read, then refreshes each member's groups.

## The sub-unit's readable ranges

A sub-unit that declares
[readable ranges](/modbus-connection/modelling/reading/#readable-address-ranges)
constrains the reads of every instance. Each instance's map resolves like its
fields — shifted by its own place in the repeat — and the maps are merged into
the plan its instances are read from.

```python
class Channel(Component):
    register_ranges = ((0, 1), (4, 5))  # 2-3 unreadable inside a channel
    a = integer(0)
    b = integer(4)


class Meter(Component):
    channels = repeating_group(2, Channel, stride=10)


# Reads 0, 4, 10 and 14 — never across the gaps the channel declares unreadable.
```

## Nesting

A `repeating_group`'s `component_class` is itself a `Component`, so it may
declare its own `repeating_group` — a sub-unit that repeats within each
instance (channels within each module, cells within each string). Nesting is
fully supported, in any combination of fixed and register counts, to any depth:
each instance's addresses shift by its parent's `stride`, and the shifts compose
additively down the levels.

```python
class Cell(Component):
    voltage = uint16(0)


class String(Component):
    cells = repeating_group(uint16(1), Cell, stride=1)  # per-string cell count


class Battery(Component):
    strings = repeating_group(uint16(0), String, stride=100)  # string count
```

A **register count** at any level adds a read pass for the level below it: the
count must be read before the instances it sizes can be planned. So a two-deep
tree with register counts at both levels polls in three passes — the outer count,
then the inner counts, then the leaves. Fixed `int` counts add no pass at any
level; they fold into the enclosing read.

## Scale factors inside the block

By default a scaled field's `scale_register` stays put across instances — it
names a shared scale factor in the parent's fixed block. A sub-unit that carries
its **own** scale factor per repeat sets the `scale_in_block` class attribute, so
each instance's scale registers shift with it:

```python
from modbus_connection.model import Component, integer, repeating_group


class Channel(Component):
    scale_in_block = True  # each channel carries its own scale factor
    a = integer(0, scale_register=1)


class Meter(Component):
    channels = repeating_group(integer(4, signed=False), Channel, stride=2)
```

Without `scale_in_block`, every channel would read its scale factor from the one
address relative to the block start; with it, channel *i*'s `scale_register`
shifts by `i * stride` like the rest of its block.
