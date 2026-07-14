---
title: SunSpec
description: SunSpec point types as ready-made model fields, pre-wired with their unimplemented sentinels and scale-factor registers.
---

[SunSpec](https://sunspec.org) defines a standard Modbus information model used by
most PV inverters, meters and batteries. Each point has a fixed data type and a
reserved *unimplemented* value the device sends when the point is absent.

`modbus_connection.model.sunspec` provides field factories that build model fields
with the right width, sign and sentinel — so an unimplemented point decodes to
`None` automatically. They are the same fields you'd otherwise hand-roll with the
[generic factories](/modbus-connection/modelling/fields/), minus the boilerplate.

```python
from modbus_connection.model import Component
from modbus_connection.model.sunspec import acc32, int16, sunssf, uint16

class Inverter(Component):
    a = uint16(2, scale_register=5)   # AC current, scaled by A_SF
    a_sf = sunssf(5)
    wh = acc32(8)                     # lifetime energy, Wh
```

Word order is big-endian throughout, per the SunSpec spec.

## Scale factors (`sunssf`)

Scaled SunSpec points reference a **scale-factor register** — a signed int16
power-of-ten exponent. Pass `scale_register=` its address, and the value is
returned as `raw * 10**sf`, with `sf` read alongside the point on each update:

```python
class Meter(Component):
    w = int16(10, scale_register=11)   # active power, scaled by W_SF
    w_sf = sunssf(11)                  # the exponent register itself
```

The scale register is read for you by the planner; you also declare it as a
`sunssf` field if you want to read its raw value directly. Writing a scaled
point works too: pass the engineering value and the scale factor is read
fresh in the same write, so a factor the device shifted meanwhile cannot
mis-scale it. A not-implemented factor raises `ValueError`.

## Numeric points

Each factory bakes in the SunSpec "unimplemented" sentinel for its type, so an
absent point decodes to `None`.

| Factory | Registers | Sentinel |
| --- | --- | --- |
| `int16` | 1 | `0x8000` |
| `uint16` | 1 | `0xFFFF` |
| `int32` | 2 | `0x80000000` |
| `uint32` | 2 | `0xFFFFFFFF` |
| `int64` | 4 | `0x8000…` |
| `uint64` | 4 | `0xFFFF…` |

```python
int16(address, *, scale=1.0, scale_register=None, scale_register_stride=0,
      stride=0, writable=False, unit=None) -> NumberField[float]
# uint16 / int32 / uint32 / int64 / uint64 share this signature.
```

## Accumulators

Accumulators are monotonic counters; SunSpec uses `0` to mean "not accumulated",
which decodes to `None`. An accumulator may reference a scale-factor register
like the numeric points do.

| Factory | Registers |
| --- | --- |
| `acc16` | 1 |
| `acc32` | 2 |
| `acc64` | 4 |

```python
acc32(address, *, scale=1.0, scale_register=None, scale_register_stride=0,
      stride=0, unit=None) -> NumberField[int]
```

## Scale-factor point

```python
sunssf(address, *, stride=0) -> NumberField[int]
```

A signed int16 power-of-ten exponent (unimplemented `0x8000`). Reference it from a
scaled point with `scale_register=`, and optionally declare it as its own field.

## Enumerations and bitfields

Pass an `IntEnum` / `IntFlag` to decode to members; omit it for the raw integer.
Both have `enum16`/`enum32` and `bitfield16`/`bitfield32`/`bitfield64` variants.

```python
enum16(address, enum=None, *, stride=0, writable=False)
enum32(address, enum=None, *, stride=0, writable=False)
bitfield16(address, flags=None, *, stride=0, writable=False)
bitfield32(address, flags=None, *, stride=0, writable=False)
bitfield64(address, flags=None, *, stride=0, writable=False)
```

```python
from enum import IntEnum
from modbus_connection.model.sunspec import enum16

class OperatingState(IntEnum):
    OFF = 1
    SLEEPING = 2
    MPPT = 4
    THROTTLED = 5

class Inverter(Component):
    st = enum16(38, OperatingState)   # decodes to a member, or None if 0xFFFF
```

## Floats and strings

```python
float32(address, *, stride=0, writable=False, unit=None) -> FloatField
float64(address, *, stride=0, writable=False, unit=None) -> FloatField
string(address, length, *, stride=0, writable=False) -> StringField
```

`float32` / `float64` decode NaN (any NaN, sentinel `0x7FC00000`) to `None`.
`string` is a fixed-length null-padded ASCII string over `length` registers.

## Address points

SunSpec models carry network addresses in registers. These are read-only:

```python
ipaddr(address, *, stride=0) -> IPv4Field     # IPv4 over 2 registers
ipv6addr(address, *, stride=0) -> IPv6Field   # IPv6 over 8 registers
eui48(address, *, stride=0) -> Eui48Field     # EUI-48 / MAC over 3 registers
```

```python
from modbus_connection.model.sunspec import ipaddr, eui48

class Comms(Component):
    ip = ipaddr(10)          # -> ipaddress.IPv4Address | None
    mac = eui48(20)          # -> str | None ("00:1a:2b:3c:4d:5e")
```

## Multiple-MPPT and other repeats

A SunSpec model advertises how many sub-blocks follow in an `N` point read at
poll time — the Multiple MPPT Inverter Extension Model (160) counts its MPPT
modules this way. Model one sub-block as a `Component` and size the list at
runtime with [`repeating_group`](/modbus-connection/modelling/repeats/).

A sub-block's scale factors can sit in the model's shared fixed block — model 160
keeps `DCA_SF`, `DCV_SF`, … there, and that is the default — or the block can
carry its **own** scale factor per repeat: declare the `sunssf` inside the
sub-block and set the sub-block's `scale_in_block` class attribute, so each
instance's scale registers shift with it.

```python
from modbus_connection.model import Component, repeating_group
from modbus_connection.model.sunspec import sunssf, uint16

class Channel(Component):
    scale_in_block = True                    # each channel carries its own scale factor
    a = uint16(0, scale_register=1)
    a_sf = sunssf(1)

class Meter(Component):
    channels = repeating_group(uint16(4), Channel, stride=2)
```

See [Repeated sub-units](/modbus-connection/modelling/repeats/) for the full story
on `base_offset`, `stride`, and `index`.

## Model discovery

A SunSpec device advertises which models it implements: a `"SunS"` marker at
the device's base address, then a chain of models, each a 2-register header
(model ID, data length) followed by that many data registers, terminated by
model ID `0xFFFF`. `scan` walks the chain:

```python
from modbus_connection.model.sunspec import scan

models = await scan(unit, 40000)   # -> dict[int, list[SunSpecModel]]
```

`base_address` is the 0-based address of the marker — the spec sanctions 0,
40000 and 50000, and an integration knows which one its manufacturer uses.
The result maps each model ID to its occurrences in chain order: the same ID
can appear more than once (e.g. several meters), and vendor models
(ID ≥ 64000) appear like any other. Each `SunSpecModel` carries `model_id`,
`address` (of the header) and `length`.

## Components at discovered models

`SunSpecComponent` is the base for a component placed at a discovered model.
Declare its fields relative to the model start — the header sits at 0/1, the
data block starts at 2 — and construct it with the discovered model:

```python
from modbus_connection.model.sunspec import SunSpecComponent, sunssf, uint16

class Inverter(SunSpecComponent):       # SunSpec model 103, relative layout
    a = uint16(2, scale_register=6)     # AC current, scaled by A_SF at data+4
    a_sf = sunssf(6)

if (found := models.get(103)) is not None:
    inv = Inverter(unit, found[0])
```

`base_offset` places every address, including `scale_register` and any
`repeating_group`. The model header is verified on every update — own or
pooled through a `ComponentGroup` — because devices shift the register map
when a configuration change resizes a model; a mismatch raises
`SunSpecMapShiftError` (a `SunSpecError`), and the owner recovers by
re-scanning and building new components at the new addresses (the read plan
is cached per instance).

## Generating components from the official definitions

SunSpec publishes every standard model as JSON in
[sunspec/models](https://github.com/sunspec/models). The generator is a helper
to get an integration started: it turns those definitions into base classes —
one `SunSpecComponent` subclass per model, with every point wired up:

```bash
python -m modbus_connection.model.sunspec.generate 1 103 160 -o sunspec_models.py
```

Arguments are model IDs (fetched from the official repository) or paths to
local `model_N.json` files; without `-o` the module prints to stdout. The
output is ordinary source, not a build artifact: commit it to your integration
as a starting point. Devices routinely deviate from the published models —
points left unimplemented, vendor quirks, off-spec sentinels or addresses —
so expect to trim and adjust the generated classes to your manufacturer's
actual implementation. Pair them with [`scan`](#model-discovery):

```python
class Model103(SunSpecComponent):
    """SunSpec model 103: Inverter (Three Phase)."""

    class St(IntEnum):  # Operating State
        OFF = 1
        SLEEPING = 2
        ...

    a = uint16(2, scale_register=6, unit='A')  # Amps
    ...
    st = enum16(38, St)  # Operating State
```

```python
models = await scan(unit, 40000)
if (found := models.get(103)) is not None:
    inverter = Model103(unit, found[0])
```

Each point becomes the matching field factory at its model-relative address:
scale-factor references become `scale_register=`, a fixed `sf` becomes a
static `scale=`, `units` and RW access carry over, and enumerated / bitfield
points get a nested `IntEnum` / `IntFlag` built from the model's symbols. The
`ID`/`L` header stays with `SunSpecComponent`'s own `model_id` /
`model_length`, and `pad` points produce no field. A repeating block becomes
its own `Component` class plus a `repeating_group` sized by the model's count
point. The few layouts that cannot be laid out statically — nested repeating
groups (some 7xx models), or a scale factor inside a repeating block — are
rejected with an error rather than generated wrong.
