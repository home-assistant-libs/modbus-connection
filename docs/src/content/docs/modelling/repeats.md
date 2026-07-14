---
title: Repeated sub-units
description: Model identical repeating sub-units with stride / index / base_offset, or size the list at runtime with repeating_group.
---

Devices that expose several identical sub-units — heating circuits, channels,
phases, MPPT modules — repeat the same registers at a fixed step. There are two
ways to model them, depending on whether the count is known when you write the
code or only at poll time.

:::tip[Which to use]
Prefer [`repeating_group`](#runtime-counted-repeats) for almost everything: it
models the sub-unit once and hands back a typed `list`, and the count can be fixed
*or* read from the device. Reach for raw `index` / `stride` only for a layout it
can't express — chiefly a sub-unit whose registers are *interleaved by type*
across the map (a different stride per field).
:::

## `index` and per-field `stride`

Model the sub-unit once and instantiate it per index — pass `index` (1-based) to
the component, and give each field a `stride` (the address step between sub-units
for *that* register). The absolute address read is
`field.address + field.stride * (index - 1)`.

Each field carries its own `stride` because devices usually group registers by
type, not by sub-unit — so one logical sub-unit's fields are interleaved across
the map at different steps:

```python
class Circuit(Component):
    flow_temp = gauge(12, 0.1, stride=1)          # circuits 1–3 at 12, 13, 14
    control_signal = integer(106, stride=2)       # ...        at 106, 108, 110
    flow_setpoint = gauge(999, 0.1, stride=200)   # ...        at 999, 1199, 1399

circuits = [Circuit(unit, index=n) for n in (1, 2, 3)]
```

A field with the default `stride=0` is at a fixed address shared by every index.

## `base_offset` — the whole layout at another address

`base_offset` places the **whole declared layout** at another base address: it
is added to every address the component touches — fields, bits, group counts
and `scale_register` addresses — on reads and writes alike. Declare the layout
once and instantiate it where the block actually sits:

```python
class Cell(Component):
    voltage = integer(0, signed=False)     # one cell; addresses are instance 0's
    temperature = gauge(1, 0.1)

cells = [Cell(unit, base_offset=i * 10) for i in range(16)]
```

The other big use is a block whose location is only known at runtime — a
[SunSpec model at its discovered address](/modbus-connection/modelling/sunspec/).
`base_offset` composes additively with `index` / `stride`.

One caution: because `base_offset` moves `scale_register` addresses with the
block, it cannot hand-roll instances of a repeating sub-unit whose scale
factors live in the parent's shared fixed block (a SunSpec multiple-MPPT
module). Model those as a `repeating_group` — a fixed `int` count works and
folds into the parent's read — and each instance shifts while its scale
registers keep following the parent's block. (`index` with a per-field
`stride` also still expresses this by hand: the scale register only moves
with `scale_register_stride`, which defaults to staying put.)

`repeating_group` is also the only way to size the list from a count the device
reports at poll time.

## Runtime-counted repeats

`stride` / `base_offset` cover repeats whose **count is known when you write the
code**. Some devices instead advertise the count in a register, read at poll time
— a SunSpec multiple-MPPT model (160) carries an `N` point saying how many modules
follow. `repeating_group` is a field for that: model one instance as a
`Component`, and the parent reads the count each poll and exposes a `list` of that
many instances, each fully typed:

```python
from modbus_connection.model import Component, integer, repeating_group
from modbus_connection.model.sunspec import uint16

class MPPTModule(Component):                 # one module, at instance 0's addresses
    dc_w = integer(11, scale_register=2)
    dc_v = integer(10, scale_register=1)

class Inverter(Component):
    modules = repeating_group(uint16(8), MPPTModule, stride=20)  # N at register 8

inv = Inverter(unit)
await inv.async_update()
inv.modules                # list[MPPTModule]
inv.modules[0].dc_w        # typed per-instance access
await inv.modules[2].write("dc_w", ...)   # writes go through the instance
```

### How the count is read

`count` is a `RegisterField` (read each poll) or a fixed `int`. Instance *i* is
read at `base_offset = i * stride`, so **`stride` is the block length**.

- A **fixed `int`** count is static, so its instances fold into the component's
  normal read — no extra pass.
- A **`RegisterField`** count needs a second pass: the count is read first, then
  the sized-out instances (pooled among themselves), since the count must be known
  before the instances it sizes can be planned.

An unimplemented or unreadable count yields no instances. A component with a
`repeating_group` can refresh on its own or be pooled in a
[`ComponentGroup`](/modbus-connection/modelling/component-group/) — the group
reads the counts in its pooled read, then refreshes each member's groups.

### Nesting

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

The signature:

```python
repeating_group(count, component_class, *, stride) -> RepeatingGroupField[C]
```

- `count` — a `RegisterField[int]` or a fixed `int` (must be `>= 0`).
- `component_class` — a `Component` subclass modelling one instance at instance
  0's addresses.
- `stride` — the block length (must be `> 0`).
