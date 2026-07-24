---
title: Manual components
description: Build a read/write group imperatively at runtime when the field layout comes from config rather than a typed class.
---

When the field layout comes from **config** (e.g. YAML) rather than a typed class
— there's no `Component` subclass to declare — use a `ManualComponent`. It's the
imperative twin of [`Component`](/modbus-connection/modelling/overview/): you
`add()` targets by key at runtime, and it pools them into as few reads as
possible, mixing all four tables (holding, input, coils, discrete inputs) in one
update.

```python
from modbus_connection.model import ManualComponent, gauge, uint32, coil, discrete_input

mc = ManualComponent(unit, max_gap=16)
mc.add("flow_temp", gauge(40, 0.1))  # holding (default)
mc.add("energy", uint32(2), space="input")  # input registers
mc.add("relay", coil(5, writable=True))  # coils (FC01)
mc.add("alarm", discrete_input(9))  # discrete inputs (FC02)

data = await mc.async_update()  # {"flow_temp": 21.5, "energy": 100000, ...}
mc.get("flow_temp")  # 21.5
await mc.write("relay", True)  # per-key write (holding / coils only)
```

## How it differs from `Component`

| | `Component` | `ManualComponent` |
| --- | --- | --- |
| Layout | class attributes, known statically | `add()`ed by key at runtime |
| Value access | typed attribute (`meter.voltage`) | `get(key)` / the dict from `async_update()` |
| Addressing | `index` / `stride` / `base_offset` | absolute `address`, no index/stride |
| Read planning | ✅ pooled | ✅ pooled (same machinery) |
| Joins a `ComponentGroup` | ✅ | ❌ |

It reuses the same planning, write (validator / `force_fc16`), bit, and
repeating-group machinery as `Component`. It just has no class to hang typed
descriptors on, so values come out by key.

## Adding and removing targets

```python
mc.add(key, target, *, space=None)
mc.remove(key)
```

- A **register** target takes its `space` on `add()` — `"holding"` (default) or
  `"input"`.
- A **bit** target's space is fixed by the factory (`coil` → FC01,
  `discrete_input` → FC02); passing `space` for a bit raises.
- A [`repeating_group`](/modbus-connection/modelling/repeats/) can be `add()`ed
  like any other target; its instances come out via `get(key)` as a `list` of
  sub-components (sized at poll time for a register count).
- The field `address` is **absolute** — there is no `index` / `stride`.

`add()` and `remove()` invalidate the cached plan, so it re-plans on the next
update. Adding a key that already exists replaces the previous target.

## Reading values

```python
mc.get("flow_temp")  # the decoded value for one key (None if not yet read)
mc.values  # a copy of every decoded value as a dict
```

`async_update()` returns the same dict it stores, so you can use either the return
value or `get()` afterwards.

## Writing

```python
await mc.write("relay", True)
```

Writes go by key and share `Component.write`'s behaviour — the `writable`
validator, and FC06 / FC16 selection with `force_fc16`. Only holding registers and
coils are writable; a discrete input or input register is read-only, so writing
one raises.

## Readable ranges

Ranges are **per-table** keyword arguments on the constructor —
`holding_ranges` / `input_ranges` / `coil_ranges` / `discrete_ranges`. Any table
left unset falls back to gap-based planning:

```python
ManualComponent(unit, holding_ranges=((0, 40),), input_ranges=((500, 520),))
```

## A worked example: config-driven fields

This is exactly the shape you want when mapping a user's configuration onto a
device. The [Home Assistant YAML page](/modbus-connection/home-assistant/yaml-configuration/)
builds a full example on top of `ManualComponent`.

```python
def build(unit, config: list[dict]) -> ManualComponent:
    mc = ManualComponent(unit)
    for entry in config:
        mc.add(
            entry["name"],
            gauge(entry["address"], entry.get("scale", 1.0)),
            space=entry.get("space", "holding"),
        )
    return mc
```
