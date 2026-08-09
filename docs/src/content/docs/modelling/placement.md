---
title: Placing a component
description: Read a declared layout at another address — a per-field stride with index, or base_offset for the whole block.
---

A component's field addresses are **declared coordinates**: where the layout sits
when it stands alone. Two knobs read that same layout somewhere else — a
per-field `stride` selected by `index`, and `base_offset` for the whole block.
They compose additively.

:::tip[Prefer a repeating group]
For several identical sub-units, reach for a
[`repeating_group`](/modbus-connection/modelling/repeats/) first: it models the
sub-unit once and hands back a typed `list`, and its count can be fixed *or*
read from the device. Place instances by hand only for a layout it can't
express — chiefly a sub-unit whose registers are *interleaved by type* across
the map, at a different stride per field.
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
    flow_temp = gauge(12, 0.1, stride=1)  # circuits 1–3 at 12, 13, 14
    control_signal = integer(106, stride=2)  # ...        at 106, 108, 110
    flow_setpoint = gauge(999, 0.1, stride=200)  # ...        at 999, 1199, 1399


circuits = [Circuit(unit, index=n) for n in (1, 2, 3)]
```

A field with the default `stride=0` is at a fixed address shared by every index.

## `base_offset` — the whole layout at another address

`base_offset` places the **whole declared layout** at another base address: it
is added to every address the component touches — fields, bits, group counts,
`scale_register` addresses and the
[readable ranges](/modbus-connection/modelling/reading/#readable-address-ranges)
— on reads and writes alike. Declare the layout once and instantiate it where the
block actually sits:

```python
class Cell(Component):
    voltage = integer(0, signed=False)  # one cell; addresses are instance 0's
    temperature = gauge(1, 0.1)


cells = [Cell(unit, base_offset=i * 10) for i in range(16)]
```

The other big use is a block whose location is only known at runtime — a
[SunSpec model at its discovered address](/modbus-connection/modelling/sunspec/).

One caution: because `base_offset` moves `scale_register` addresses with the
block, it cannot hand-roll instances of a repeating sub-unit whose scale factors
live in the parent's shared fixed block (a SunSpec multiple-MPPT module). Model
those as a [`repeating_group`](/modbus-connection/modelling/repeats/), where each
instance shifts while its scale registers keep following the parent's block.
