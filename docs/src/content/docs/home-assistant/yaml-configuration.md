---
title: Modbus YAML configuration
description: Translate a Home Assistant Modbus YAML configuration into a typed Component when building a device library.
---

Home Assistant's [Modbus integration](https://www.home-assistant.io/integrations/modbus)
is configured in YAML: each sensor names an `address`, a `data_type`, an
`input_type`, and value shaping like `scale`, `offset` and `swap`. Someone who
already runs a device through Home Assistant's Modbus integration has, in that
YAML, a **ready-made description of the device's register map**.

That makes it an excellent starting point for a
[device library](/modbus-connection/patterns/library/): translate each YAML entry
into a typed [field](/modbus-connection/modelling/fields/) on a
[`Component`](/modbus-connection/modelling/overview/). The mapping is almost
one-to-one.

:::note[This is a translation, not a loader]
The goal here is to turn a YAML config into a **`Component` class you commit as
source** — part of building a proper library, with typed attributes and IDE
completion. It is not about loading YAML at runtime. (If you genuinely must build
the layout from config the program only sees at runtime, that's what
[`ManualComponent`](/modbus-connection/modelling/manual-component/) is for.)
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
| `input_type: holding` | default register space |
| `input_type: input` | `register_space = "input"` on the component |
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

## The resulting component

Translating the two sensors above gives a typed class. Because holding and input
are separate spaces, the input-register sensor moves to its own component (or, if
you have many, group them with a [`ComponentGroup`](/modbus-connection/modelling/component-group/)):

```python
from modbus_connection.model import Component, gauge, uint32


class Hub1(Component):
    # input_type: holding (the default space)
    outside_temperature = gauge(9, 0.1, unit="°C")  # int16, scale 0.1


class Hub1Inputs(Component):
    register_space = "input"  # input_type: input
    energy = uint32(2, word_order="little", unit="Wh")  # swap: word -> CDAB
```

Now every value is a typed attribute, checked by your type checker and completed
by your editor — the payoff of translating to a class rather than reading dicts:

```python
hub = Hub1(unit)
await hub.async_update()
hub.outside_temperature  # float | None
```

The register map now lives in code as the datasheet: addresses, scales, units and
ranges all sit next to the attribute they describe.

## Caveats worth knowing

A few Home Assistant options don't map to a single field helper. Handle them as
you translate:

- **`precision`** — Home Assistant rounds the *display* value to this many
  decimals. modbus-connection rounds by the decimals implied by `scale`, so apply
  `precision` in a `@property` if you need Home Assistant's exact rounding.
- **`swap: byte` / `swap: word_byte`** — these swap *bytes within* a register.
  The field helpers model **word** order (`word_order`), not byte order, which
  is fixed big-endian. A byte-swapping device needs a custom
  [`RegisterField` subclass](/modbus-connection/modelling/fields/#when-the-helpers-dont-fit).
- **`data_type: float16`** — not a built-in codec; decode the raw word with a
  `raw_register` and convert in a `@property`.
- **`data_type: custom` + `structure`** — a Python `struct` format string. Read
  the raw words with `raw_register`(s) and unpack them in a `@property`.
- **`slave` / `device_address`** — this is the **unit id**. Pick it when you build
  the `ModbusUnit` with `connection.for_unit(slave)`, not on a field.
- **`virtual_count` / `slave_count`** — Home Assistant fans one entry out into
  several consecutive entities. Model it with a
  [repeated sub-unit](/modbus-connection/modelling/repeats/) (`stride`) or one
  field per index.

## Writable entities

Home Assistant `switch`, `climate`, `number` and `select` entities write back.
Mark the corresponding field `writable` (and add a
[validator](/modbus-connection/modelling/fields/#writable-fields-and-validators)
to enforce `min_value` / `max_value`), then write by attribute name:

```python
from modbus_connection.model import Component, coil, gauge


def _clamp(value: float) -> float:
    if not 10 <= value <= 30:
        raise ValueError(f"{value} out of range")
    return value


class Climate(Component):
    relay = coil(5, writable=True)  # a switch entity
    target_temp = gauge(40, 0.1, writable=_clamp)  # a number entity


await climate.write("relay", True)
await climate.write("target_temp", 21.5)
```

Some devices honour only FC16 for writes — Home Assistant's `write_type:
holdings` — so pass `force_fc16=True` on the field.
