---
title: Placing a component
description: Read a declared layout at another address, with a per-field stride selected by index or with base_offset for the whole block.
---

A component's field addresses are declared coordinates: where the layout sits
when it stands alone. A per-field `stride` selected by `index` reads that same
layout somewhere else, and so does `base_offset` for the whole block. The two
compose additively.

:::tip[Prefer a repeating group]
For several identical sub-units, use a
[`repeating_group`](/modbus-connection/modelling/repeats/) first. It models the
sub-unit once and returns a typed `list`, and its count can be fixed or read
from the device. Place instances by hand only for a layout it cannot express,
chiefly a sub-unit whose registers are interleaved by type across the map, at
a different stride per field.
:::

## `index` and per-field `stride`

Model the sub-unit once and instantiate it per index. Pass `index` (1-based) to
the component, and give each field a `stride`, the address step between
sub-units for that register. The absolute address read is
`field.address + field.stride * (index - 1)`.

Each field carries its own `stride` because devices usually group registers by
type rather than by sub-unit. One logical sub-unit's fields are then interleaved
across the map at different steps:

```python
class Circuit(Component):
    flow_temp = gauge(12, 0.1, stride=1)  # circuits 1–3 at 12, 13, 14
    control_signal = integer(106, stride=2)  # ...        at 106, 108, 110
    flow_setpoint = gauge(999, 0.1, stride=200)  # ...        at 999, 1199, 1399


circuits = [Circuit(unit, index=n) for n in (1, 2, 3)]
```

A field with the default `stride=0` sits at a fixed address shared by every
index.

## `base_offset` for the whole layout

`base_offset` places the whole declared layout at another base address. It is
added to every address the component touches, on reads and writes alike:
fields, bits, group counts, `scale_register` addresses and the
[readable ranges](/modbus-connection/modelling/reading/#readable-address-ranges).
Declare the layout once and instantiate it where the block sits:

```python
class Cell(Component):
    voltage = integer(0, signed=False)  # one cell; addresses are instance 0's
    temperature = gauge(1, 0.1)


cells = [Cell(unit, base_offset=i * 10) for i in range(16)]
```

The other main use is a block whose location is only known at runtime, such as
a [SunSpec model at its discovered address](/modbus-connection/modelling/sunspec/).

`base_offset` moves `scale_register` addresses with the block, so it cannot
hand-roll instances of a repeating sub-unit whose scale factors live in the
parent's shared fixed block (a SunSpec multiple-MPPT module). Model those as a
[`repeating_group`](/modbus-connection/modelling/repeats/): each instance
shifts while its scale registers keep following the parent's block.
