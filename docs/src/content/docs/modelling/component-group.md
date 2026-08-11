---
title: Component groups
description: Refresh several components that share a unit in one consolidated set of pooled Modbus reads.
---

A **`ComponentGroup`** stands in for its members at update time: it takes a list
of components instead of declaring fields, and offers the same
`async_update()` / `async_read_raw()` / `notify()` surface over one pooled set of
block reads spanning every member. Fields, writes and listeners stay on the
members themselves.

A physical device is usually several sub-systems on one unit — a water heater,
three heating circuits, a set of sensors — and polling each one separately means
many small Modbus reads where a few larger ones would do. A group fetches
adjacent registers from *different* components in the same Modbus call, and each
component's listeners still fire after the update.

```python
from modbus_connection.model import ComponentGroup

group = ComponentGroup(unit, [water_heater, circuit_1, circuit_2, circuit_3])
await group.async_update()  # one pooled set of reads; each component notified
```

## How it plans

The `ComponentGroup` builds its pooled read plan from the components' static
layout and reuses it on every later poll. Reshaping a member through
[`restrict_fields`](/modbus-connection/modelling/restricting-fields/) is
supported at any time — it invalidates the pooled plan, which is rebuilt from
the narrowed layout on the next update. The member list itself is fixed at
construction, and assigning to a member's layout attributes directly (its
fields or range tuples) bypasses that invalidation and is not supported; build
a new `ComponentGroup` (or `Component`) for those.

Because reads across a group are pooled, `read_holding_registers` is called once
per contiguous block spanning whichever components fall in it, not once per
component. For a device with dozens of scattered fields this typically collapses
tens of reads into a handful.

Pooling never widens a read beyond what the members already licensed. For a
member that declared no
[readable ranges](/modbus-connection/modelling/reading/#readable-address-ranges),
its addresses merge exactly as they would if it refreshed alone, and it shares a
block with another member only where their blocks meet. Where a member *did*
declare ranges, that map applies to the pooled read as it does to a solo one:
inside a range the planner still merges freely over registers no field claims.

## Shared configuration

The readable address ranges and planning limits come from the **components** —
they describe one device's address map — so components in a group must agree:

- Readable ranges apply **per address space**, and the group merges what its
  members declare for each one — resolved to the addresses they actually read, so
  a member placed with
  [`base_offset`](/modbus-connection/modelling/placement/) contributes its shifted
  map. Members at different offsets each describe their own part of the device.
- Two members whose resolved ranges **overlap without matching** describe the same
  addresses two different ways, which raises `ValueError`.
- Every component must share `max_span`, which caps a pooled block's width.

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

## When a block read fails

Group updates fail the same way individual ones do: if the device answers one of
the pooled block reads with a Modbus exception response, `async_update()` raises
the [typed exception](/modbus-connection/connection/reference/#modbusexceptionerror) for its code — here
for *any* block across the pooled members. See [When a block read
fails](/modbus-connection/modelling/reading/#when-a-block-read-fails) for what the
exception carries.

## When to use which

- One sub-system, or sub-systems polled on different schedules → individual
  `Component.async_update()`.
- Several sub-systems of one device polled together → a `ComponentGroup`.
- A layout not known until runtime (from config) → a
  [`ManualComponent`](/modbus-connection/modelling/manual-component/).
