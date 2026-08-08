---
title: Component groups
description: Refresh several components that share a unit in one consolidated set of pooled Modbus reads.
---

Each `Component` can refresh independently. But a physical device is usually
several sub-systems on one unit — a water heater, three heating circuits, a set of
sensors — and polling each one separately means many small Modbus reads where a
few larger ones would do.

A **`ComponentGroup`** pools several components on one unit into a single
consolidated set of block reads: adjacent registers from *different* components
are fetched in the same Modbus call. Each component's listeners still fire after
the update.

```python
from modbus_connection.model import ComponentGroup

group = ComponentGroup(unit, [water_heater, circuit_1, circuit_2, circuit_3])
await group.async_update()  # one pooled set of reads; each component notified
```

## How it plans

The `ComponentGroup` builds its pooled read plan from the components' static
layout on the **first update** and reuses it on every later poll. The component
list, their fields, and the ranges are read once and cached — **mutating them
after the first update is not supported**; build a new `ComponentGroup` (or
`Component`) instead.

Because reads across a group are pooled, `read_holding_registers` is called once
per contiguous block spanning whichever components fall in it, not once per
component. For a device with dozens of scattered fields this typically collapses
tens of reads into a handful.

## Mixing spaces

Components in a group may mix register spaces and bit spaces. An input (FC04)
sub-system and a holding (FC03) sub-system in the same group are read with
separate block reads; likewise coils (FC01) and discrete inputs (FC02). Each space
is planned and read on its own.

## Shared configuration

The readable address ranges and planning limits come from the **components** —
they describe one device's address map — so components in a group must agree:

- Readable ranges apply **per address space**, and the group merges what its
  members declare for each one — resolved to the addresses they actually read, so
  a member placed with
  [`base_offset`](/modbus-connection/modelling/repeats/) contributes its shifted
  map. Members at different offsets each describe their own part of the device.
- Two members whose resolved ranges **overlap without matching** describe the same
  addresses two different ways, and a member that constrains a space cannot be
  pooled with one that leaves it unset. Either raises `ValueError`.
- Every component must share `max_gap` and `max_span`.

The range rules are a guard: a group is one device, so its members can't disagree
about that device's map.

```python
class Base(Component):
    register_ranges = ((0, 6), (9, 40))
    coil_ranges = ((0, 15),)


class WaterHeater(Base): ...


class Circuit(Base): ...


# All share the same ranges, so the group accepts them.
group = ComponentGroup(unit, [WaterHeater(unit), Circuit(unit, index=1)])
await group.async_update()
```

## Repeating groups inside a group

If a member declares a [`repeating_group`](/modbus-connection/modelling/repeats/),
the group handles it: the pooled read fetches each member's own fields plus any
repeating-group counts, then a second pass sizes and reads each member's
register-count groups. So runtime-counted repeats refresh inside the group too.

## When a block read fails

Group updates fail the same way individual ones do: if the device answers one of
the pooled block reads with a Modbus exception response, `async_update()` raises
[`BlockReadError`](/modbus-connection/connection/reference/#blockreaderror-deprecated) — here
for *any* block across the pooled members. See [When a block read
fails](/modbus-connection/modelling/overview/#when-a-block-read-fails) for what the
exception carries.

## Reading without notifying

Pass `notify=False` to read every component without firing their listeners, for a
caller that notifies them itself:

```python
await group.async_update(notify=False)
```

## When to use which

- One sub-system, or sub-systems polled on different schedules → individual
  `Component.async_update()`.
- Several sub-systems of one device polled together → a `ComponentGroup`.
- A layout not known until runtime (from config) → a
  [`ManualComponent`](/modbus-connection/modelling/manual-component/), which does
  its own pooling but does not join a `ComponentGroup`.
