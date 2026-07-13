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
`sunssf` field if you want to read its raw value directly. Writing a
dynamically-scaled point is unsupported.

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
which decodes to `None`.

| Factory | Registers |
| --- | --- |
| `acc16` | 1 |
| `acc32` | 2 |
| `acc64` | 4 |

```python
acc32(address, *, scale=1.0, stride=0, unit=None) -> NumberField[int]
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

A SunSpec model 160 (multiple MPPT) advertises how many modules follow in an `N`
point read at poll time. Model one module as a `Component` and size the list at
runtime with [`repeating_group`](/modbus-connection/modelling/repeats/):

```python
from modbus_connection.model import Component, integer, repeating_group
from modbus_connection.model.sunspec import uint16

class MPPTModule(Component):                 # one module, at instance 0's addresses
    dc_w = integer(11, scale_register=2)
    dc_v = integer(10, scale_register=1)

class Inverter(Component):
    modules = repeating_group(uint16(8), MPPTModule, stride=20)  # N at register 8
```

A repeating block's scale factors sit in the shared fixed block, so a
`repeating_group` instance shifts per instance while its `scale_register`
addresses keep following the model — see
[Repeated sub-units](/modbus-connection/modelling/repeats/) for the full story on
`base_offset`, `stride`, and `index`.

## Models at discovered addresses

SunSpec models are found by walking the model chain, so their addresses are
only known at runtime — and can even shift when a device setting changes the
register map. Declare the model relative to its start (address 0) and place it
with `base_offset`; every address moves with it, including `scale_register`
addresses and any `repeating_group`:

```python
class Inverter(Component):              # SunSpec model 103, relative layout
    a = uint16(0, scale_register=4)     # AC current, scaled by A_SF at +4
    a_sf = sunssf(4)

model_address = ...                     # from walking the model chain
inv = Inverter(unit, base_offset=model_address + 2)  # data follows the header
```

The read plan is cached per instance, so when the map shifts, re-discover and
build a new component with the new `base_offset`.
