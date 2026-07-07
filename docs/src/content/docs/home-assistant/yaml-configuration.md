---
title: Modbus YAML configuration
description: Turn Home Assistant's Modbus YAML sensor configuration into modbus-connection fields with a ManualComponent.
---

Home Assistant's [Modbus integration](https://www.home-assistant.io/integrations/modbus)
is configured in YAML: each sensor names an `address`, a `data_type`, an
`input_type`, and value shaping like `scale`, `offset` and `swap`. That maps
almost one-to-one onto modbus-connection's [field factories](/modbus-connection/modelling/fields/).

This page shows how to consume that YAML and build a
[`ManualComponent`](/modbus-connection/modelling/manual-component/) from it — the
right tool because the layout comes from config, not a typed class.

:::note[Not tied to Home Assistant]
Nothing here imports Home Assistant. The YAML *shape* is Home Assistant's, but the
mapping is plain Python — the same approach works for any config-driven field
layout.
:::

## A Home Assistant Modbus sensor

```yaml
modbus:
  - name: hub1
    type: tcp
    host: 192.168.1.50
    port: 502
    sensors:
      - name: outside_temperature
        address: 9
        input_type: holding
        data_type: int16
        scale: 0.1
        offset: 0
        unit_of_measurement: "°C"
      - name: energy
        address: 2
        input_type: input
        data_type: uint32
        swap: word
        unit_of_measurement: "Wh"
```

## Mapping the keys

| Home Assistant key | modbus-connection |
| --- | --- |
| `address` | the field's `address` |
| `input_type: holding` | `space="holding"` (register default) |
| `input_type: input` | `space="input"` |
| `data_type: int16` / `uint16` | `integer(..., signed=…)` |
| `data_type: int32` / `uint32` | `int32(...)` / `uint32(...)` |
| `data_type: int64` / `uint64` | `int64(...)` / `uint64(...)` |
| `data_type: float32` / `float64` | `float32(...)` / `float64(...)` |
| `data_type: string` + `count` | `string(address, count)` |
| `scale` (with `data_type` numeric) | `gauge(address, scale)` |
| `offset` | `offset=` |
| `swap: none` / `swap: word` | `word_order="big"` / `"little"` |
| `unit_of_measurement` | `unit=` |

A sensor with a non-default `scale` becomes a `gauge` (which carries the scale); a
plain integer with `scale: 1` becomes an `integer`. Coil / discrete-input entities
(Home Assistant switches and binary sensors) map to `coil` and `discrete_input`.

## A mapping function

```python
from modbus_connection.model import (
    ManualComponent, integer, gauge, uint32, int32, uint64, int64,
    float32, float64, string,
)

# data_type -> (factory builder). Numeric builders take scale/offset/unit.
def _numeric_field(entry: dict):
    data_type = entry["data_type"]
    address = entry["address"]
    scale = entry.get("scale", 1)
    offset = entry.get("offset", 0)
    unit = entry.get("unit_of_measurement")
    # "word" swaps the 16-bit words (CDAB); "none" keeps big-endian (ABCD).
    word_order = "little" if entry.get("swap") == "word" else "big"

    if data_type in ("int16", "uint16"):
        signed = data_type == "int16"
        if scale != 1 or offset:
            return gauge(address, scale, offset=offset, signed=signed, unit=unit)
        return integer(address, offset=offset, signed=signed, unit=unit)

    multi = {
        "int32": int32, "uint32": uint32,
        "int64": int64, "uint64": uint64,
        "float32": float32, "float64": float64,
    }
    factory = multi[data_type]
    return factory(address, scale=scale, offset=offset,
                   word_order=word_order, unit=unit)


def build_component(unit, sensors: list[dict]) -> ManualComponent:
    """Build a ManualComponent from Home Assistant modbus sensor config."""
    mc = ManualComponent(unit)
    for entry in sensors:
        space = "input" if entry.get("input_type") == "input" else "holding"
        if entry["data_type"] == "string":
            field = string(entry["address"], entry["count"])
        else:
            field = _numeric_field(entry)
        mc.add(entry["name"], field, space=space)
    return mc
```

Then read the device and hand the values back to Home Assistant:

```python
mc = build_component(unit, config["sensors"])
values = await mc.async_update()
# {"outside_temperature": 4.2, "energy": 100000, ...}
```

Every sensor across the whole config is pooled into as few Modbus reads as the
address map allows — exactly what you want when a hub has dozens of sensors.

## Caveats worth knowing

A few Home Assistant options don't map to a single field factory. Handle them in
your consumer:

- **`precision`** — Home Assistant rounds the *display* value to this many
  decimals. modbus-connection rounds by the decimals implied by `scale`, so apply
  `precision` yourself after decoding if you need Home Assistant's exact rounding.
- **`swap: byte` / `swap: word_byte`** — these swap *bytes within* a register.
  The field factories model **word** order (`word_order`), not byte order, which
  is fixed big-endian. A byte-swapping device needs a custom
  [`RegisterField` subclass](/modbus-connection/modelling/fields/#field-classes).
- **`data_type: float16`** — not a built-in codec; decode the raw word with a
  `raw_register` and convert.
- **`data_type: custom` + `structure`** — a Python `struct` format string. Read
  the raw words with `raw_register`(s) and unpack them in a `@property`.
- **`slave` / `device_address`** — this is the **unit id**. Pick it when you build
  the `ModbusUnit` with `connection.for_unit(slave)`, not on the field.
- **`virtual_count` / `slave_count`** — Home Assistant fans one entry out into
  several consecutive entities. Model it with a
  [repeated sub-unit](/modbus-connection/modelling/repeats/) (`stride`) or by
  adding one field per index.

## Writable entities

Home Assistant `switch`, `climate`, `number` and `select` entities write back.
Mark the corresponding field `writable` (and add a
[validator](/modbus-connection/modelling/fields/#writable-fields-and-validators)
to enforce `min_value` / `max_value`), then write by key:

```python
mc.add("relay", coil(5, writable=True))
await mc.write("relay", True)
```

Some devices honour only FC16 for writes — Home Assistant's `write_type:
holdings` — so pass `force_fc16=True` on the field.
