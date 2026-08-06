---
title: SunSpec fields
description: SunSpec point types as ready-made model fields, pre-wired with their unimplemented sentinels and scale-factor registers.
---

[SunSpec](https://sunspec.org) defines a standard Modbus information model used by
most PV inverters, meters and batteries. Each point has a fixed data type and a
reserved *unimplemented* value the device sends when the point is absent.

`modbus_connection.model.sunspec` provides helpers that build model fields
with the right width, sign and sentinel — so an unimplemented point decodes to
`None` automatically. They are the same fields you'd otherwise hand-roll with the
[generic fields](/modbus-connection/modelling/fields/), minus the boilerplate;
the full signatures live in the
[field reference](/modbus-connection/modelling/fields-reference/#sunspec-point-helpers).

```python
from modbus_connection.model import Component
from modbus_connection.model.sunspec import acc32, int16, sunssf, uint16


class Inverter(Component):
    a = uint16(2, scale_register=5)  # AC current, scaled by A_SF
    a_sf = sunssf(5)
    wh = acc32(8)  # lifetime energy, Wh
```

Word order is big-endian throughout, per the SunSpec spec.

## Scale factors (`sunssf`)

Scaled SunSpec points reference a **scale-factor register** — a signed int16
power-of-ten exponent. Pass `scale_register=` its address, and the value is
returned as `raw * 10**sf`, with `sf` read alongside the point on each update:

```python
class Meter(Component):
    w = int16(10, scale_register=11)  # active power, scaled by W_SF
    w_sf = sunssf(11)  # the exponent register itself
```

The scale register is read for you by the planner; you also declare it as a
`sunssf` field if you want to read its raw value directly. Writing a scaled
point works too: pass the engineering value and the scale factor is read
fresh in the same write, so a factor the device shifted meanwhile cannot
mis-scale it. A not-implemented factor raises `ValueError`.

The spec constrains a `sunssf` exponent to **-10..10**. Devices have been seen
reporting garbage exponents outside that range (typically around an inverter's
sleep/wake transition), which would scale a sane raw value into an absurd
reading. A point whose exponent falls outside the spec range therefore decodes
to `None`, and a write with one raises `ValueError`.

## Numeric points

Each helper bakes in the SunSpec "unimplemented" sentinel for its type, so an
absent point decodes to `None`.

| Helper | Registers | Sentinel |
| --- | --- | --- |
| `int16` | 1 | `0x8000` |
| `uint16` | 1 | `0xFFFF` |
| `int32` | 2 | `0x80000000` |
| `uint32` | 2 | `0xFFFFFFFF` |
| `int64` | 4 | `0x8000…` |
| `uint64` | 4 | `0xFFFF…` |

All six share one signature: `address`, then keyword-only `scale`,
`scale_register` / `scale_register_stride`, `stride`, `writable`, and `unit`.

## Accumulators

Accumulators are monotonic counters; SunSpec uses `0` to mean "not accumulated",
which decodes to `None`. An accumulator may reference a scale-factor register
like the numeric points do.

| Helper | Registers |
| --- | --- |
| `acc16` | 1 |
| `acc32` | 2 |
| `acc64` | 4 |

They take the numeric points' options minus `writable` — a counter is never
written.

## Scale-factor point

`sunssf` is a signed int16 power-of-ten exponent (unimplemented `0x8000`).
Reference it from a scaled point with `scale_register=`, and optionally declare
it as its own field.

## Enumerations and bitfields

Pass an `IntEnum` / `IntFlag` to decode to members; omit it for the raw integer.
Both have `enum16`/`enum32` and `bitfield16`/`bitfield32`/`bitfield64` variants.

```python
from enum import IntEnum
from modbus_connection.model.sunspec import enum16


class OperatingState(IntEnum):
    OFF = 1
    SLEEPING = 2
    MPPT = 4
    THROTTLED = 5


class Inverter(Component):
    st = enum16(38, OperatingState)  # decodes to a member, or None if 0xFFFF
```

## Floats and strings

`float32` / `float64` decode NaN (any NaN, sentinel `0x7FC00000`) to `None`.
`string(address, length)` is a fixed-length null-padded ASCII string over
`length` registers.

## Address points

SunSpec models carry network addresses in registers. These are read-only:
`ipaddr` (IPv4 over two registers), `ipv6addr` (IPv6 over eight), and `eui48`
(an EUI-48 / MAC address over three).

```python
from modbus_connection.model.sunspec import ipaddr, eui48


class Comms(Component):
    ip = ipaddr(10)  # -> ipaddress.IPv4Address | None
    mac = eui48(20)  # -> str | None ("00:1a:2b:3c:4d:5e")
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
    scale_in_block = True  # each channel carries its own scale factor
    a = uint16(0, scale_register=1)
    a_sf = sunssf(1)


class Meter(Component):
    channels = repeating_group(uint16(4), Channel, stride=2)
```

See [Repeated sub-units](/modbus-connection/modelling/repeats/) for the full story
on `base_offset`, `stride`, and `index`.

Continue with
[Discovery and generation](/modbus-connection/modelling/sunspec-discovery/) to
locate models on a device and generate component classes for them.
