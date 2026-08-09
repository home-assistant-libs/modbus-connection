---
title: Restricting fields
description: Narrow a typed Component to the subset of a layout a device actually serves, when the served registers vary by firmware.
---

Some devices serve only a **subset** of a known layout, and which registers they
answer depends on the firmware — often with no model or version register to key
off. The layout is otherwise perfectly typed; only the served subset varies per
device.

A block read is atomic, so this is a problem: a single unserved register *inside* a
block [fails the whole read](/modbus-connection/modelling/reading/#when-a-block-read-fails),
taking every other field in that block down with it. Splitting the layout across
[separate components](/modbus-connection/modelling/component-group/) doesn't help
when the served and unserved registers are interleaved.

## `restrict_fields`

`Component.restrict_fields(names)` narrows a component to the named fields, keeping
the typed layer intact for the ones that remain:

```python
class Boiler(Component):
    register_ranges = ((0, 5), (50, 50))

    flow_temperature = gauge(0, 0.1, unit="°C")
    return_temperature = gauge(1, 0.1, unit="°C")
    high_temperature = gauge(2, 0.1, unit="°C")  # only some firmwares serve this
    pressure = gauge(3, 0.1, unit="bar")
    flow_rate = gauge(4, 0.1, unit="m³/h")
    pump_speed = integer(5, unit="%")
    mode = integer(50, writable=True)


boiler = Boiler(unit)
boiler.restrict_fields(["flow_temperature", "return_temperature", "pressure", "mode"])
await boiler.async_update()  # reads only the kept fields' registers
```

The kept fields keep typed attribute access (`boiler.flow_temperature`), a stock
`async_update()`, and typed `write()`. An excluded field reads as `None` and can no
longer be written.

Crucially, this also constrains the **read plan**, not just the field set — a block
read can never span an excluded register, which is what stops the update failing on
a firmware that omits one. (Excluding a field on its own would not achieve this: the
planner still pools a block across the gap between the fields it keeps.)

## Determining the served set

Which fields to keep is **up to you** — the library deliberately doesn't decide,
because how a device reveals its layout is device-specific. Two common approaches:

- **Probe once at setup** — read each declared range, falling back to
  single-register reads on a refusal, and keep the fields that answered. This
  belongs to a library's [setup](/modbus-connection/patterns/library/#the-shape),
  not its polling path.
- **Look it up** — if the device reports a model or firmware version somewhere, map
  that to a known field set.

Either way you end up with the list of field names to pass to `restrict_fields`.
Both approaches start from the names the component declares: `Component.declared_fields`
is a read-only mapping of attribute name to field object, in declaration order, on
the class as well as on an instance. `restrict_fields` never narrows it, so it keeps
describing the full declared layout afterwards.

## Limitations

`restrict_fields` raises `ValueError` for an unknown field name, and on a component
that declares a [`repeating_group`](/modbus-connection/modelling/repeats/), which is
not supported.

A [`SunSpecComponent`](/modbus-connection/modelling/sunspec-discovery/) keeps its
`model_id` and `model_length` fields whichever names you pass: every update
verifies that header, so a restriction that dropped it would fail each update
instead of narrowing the component.
