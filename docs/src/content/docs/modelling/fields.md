---
title: Built-in fields
description: Every generic field helper — integers, gauges, floats, strings, enums, flags, coils and discrete inputs — with its options.
---

A **field** is a descriptor you place on a `Component`. It owns the codec (how raw
register words become a Python value) but holds no per-read state. Reading the
attribute returns `T | None` — the decoded value, or `None` before the first read
or when a sentinel decodes to "no value".

Prefer the **helpers** below over constructing field classes directly; they are
named presets (width, sign, sentinel, scale) over a small set of codecs. Every
helper lives in `modbus_connection.model`:

```python
from modbus_connection.model import (
    integer,
    gauge,
    raw_register,
    uint32,
    int32,
    uint64,
    int64,
    float32,
    float64,
    string,
    enum,
    flags,
    coil,
    discrete_input,
)
```

## Options shared across register fields

Most register helpers accept these keyword arguments. Not every option applies
to every helper (a `string` has no `scale`, for instance) — the tables below list
what each one takes.

| Option | Meaning |
| --- | --- |
| `address` | Address of the value's first register word (before `stride`/`base_offset`). |
| `count` | Number of 16-bit registers the value spans (fixed by most helpers). |
| `scale` | Affine multiplier: the value decodes as `raw * scale + offset`. |
| `offset` | Affine addend, for a device that reports a shifted value. |
| `nan` | Raw sentinel value — or several — that decode to `None` (device "unimplemented"). |
| `signed` | Interpret the raw integer as two's-complement. |
| `word_order` | `"big"` (default, ABCD) or `"little"` (CDAB) for multi-register values. |
| `unit` | Unit-of-measure label carried as metadata; not used in decoding. |
| `stride` | Per-index address step for a [repeated sub-unit](/modbus-connection/modelling/repeats/). |
| `writable` | `True`, or a [validator callable](#writable-fields-and-validators). |
| `force_fc16` | Always write with FC16, even a single register. Requires `writable`. |
| `scale_register` | Address of a [SunSpec scale-factor register](/modbus-connection/modelling/sunspec/) applied as `10**sf`. |
| `scale_register_stride` | Per-index step for `scale_register`. |

### Affine scaling

Numeric fields decode as `raw * scale + offset`. Pass `offset` for a device that
reports a shifted value — e.g. `gauge(0, 0.1, offset=-100)` for a temperature
stored as `raw * 0.1 - 100`. Writable fields invert it as `(value - offset) / scale`.

### The `nan` sentinel

Many devices send a reserved value to mean "this point is not implemented". Pass
`nan=` that raw value and the field decodes it to `None`:

```python
temperature = gauge(5, 0.1, nan=0x8000)  # 0x8000 -> None
```

A device may define several distinct "no value" codes for the same register — one
for an absent register, another for an unplugged probe. Pass all of them:

```python
temperature = gauge(5, 0.1, nan=(0x8000, 0xF448))  # either -> None
```

The values are matched against the **raw** register word, before `signed` is
applied, so state them as they appear on the wire (`0xF448`, not `-3000`).

### Word order

`word_order` selects the order of the 16-bit registers in a multi-register value.
It defaults to `"big"` (the Modbus convention), covering the ABCD arrangement;
pass `"little"` for CDAB.

---

## Numeric fields

### `integer`

An unscaled integer register — counts, percentages, addresses.

```python
integer(address, *, offset=0.0, signed=True, nan=None, stride=0,
        writable=False, scale_register=None, scale_register_stride=0,
        unit=None, force_fc16=False) -> NumberField[int]
```

```python
count = integer(4)  # signed 16-bit int
percent = integer(7, signed=False)  # 0..65535
shifted = integer(2, offset=-100)  # raw - 100
```

### `gauge`

A scaled numeric register — a 0.1-scaled temperature, a voltage, and so on. The
one helper where `scale` is a **required positional** argument.

```python
gauge(address, scale, *, offset=0.0, signed=True, nan=None, stride=0,
      writable=False, scale_register=None, scale_register_stride=0,
      unit=None, force_fc16=False) -> NumberField[float]
```

```python
voltage = gauge(0, 0.1, unit="V")  # raw * 0.1
temp = gauge(9, 0.1, offset=-40, unit="°C")  # raw * 0.1 - 40
```

### `raw_register`

A single raw register word — no scaling, sign handling, or sentinel. Useful for a
status word you decode yourself.

```python
raw_register(address, *, stride=0, writable=False, force_fc16=False) -> RawField
```

### 32- and 64-bit integers

`uint32` / `int32` span two consecutive registers; `uint64` / `int64` span four.
All take `scale`, `offset`, `word_order`, `unit`, and the write options.

```python
uint32(address, *, scale=1.0, offset=0.0, word_order="big", stride=0,
       writable=False, unit=None, force_fc16=False) -> NumberField[int]
# int32 / uint64 / int64 have the same signature.
```

```python
energy = uint32(2, unit="Wh")  # 32-bit over registers 2–3
signed_power = int32(10, word_order="little")  # CDAB word order
lifetime = uint64(20, unit="Wh")  # 64-bit over registers 20–23
```

## Floating-point fields

`float32` decodes an IEEE-754 single over two registers; `float64` a double over
four.

```python
float32(address, *, scale=1.0, offset=0.0, word_order="big", stride=0,
        writable=False, unit=None, force_fc16=False) -> FloatField
# float64 is identical but spans four registers.
```

```python
flow = float32(40, unit="m³/h")
precise = float64(50)
```

## String fields

`string` reads a fixed-length null-padded ASCII string over `length` registers
(two characters per register).

```python
string(address, length, *, stride=0, writable=False, force_fc16=False) -> StringField
```

```python
serial = string(100, 8)  # 8 registers -> up to 16 ASCII characters
```

## Enum and flag fields

Map a raw register natively to an `IntEnum` or `IntFlag`.

- `enum` — an `IntEnum` field. A code with no member decodes to `None` (warned
  once per distinct value).
- `flags` — an `IntFlag` field. Unknown bits are **kept**.

```python
enum(address, enum_type, *, count=1, signed=False, word_order="big",
     nan=None, stride=0, writable=False, force_fc16=False) -> NumberField[E]
flags(address, flag_type, *, count=1, signed=False, word_order="big",
      nan=None, stride=0, writable=False, force_fc16=False) -> NumberField[F]
```

```python
from enum import IntEnum, IntFlag


class Mode(IntEnum):
    OFF = 0
    HEAT = 1
    COOL = 2


class Alarms(IntFlag):
    OVERTEMP = 1
    UNDERVOLT = 2


class Device(Component):
    mode = enum(3, Mode)
    alarms = flags(4, Alarms)
```

`signed` interprets the code as two's-complement for devices with negative enum
codes (e.g. `-1` sent as `0xFFFF`); the default is unsigned.

Under the hood both helpers pass the enum class to `NumberField(convert=...)`,
which accepts any `Callable[[int], T]` — an enum class is just a callable that
raises `ValueError` for unknown codes — or a `Mapping[int, T]`, where a missing
key means the same. Either way an unknown value decodes to `None`, warned once
per distinct value. For a mapping an enum class can't express (e.g. onto a
`StrEnum`), pass the dict inline:

```python
from enum import StrEnum


class State(StrEnum):
    OFF = "off"
    RUNNING = "running"


class Device(Component):
    state: NumberField[State] = NumberField(
        5, signed=False, convert={1: State.OFF, 4: State.RUNNING}
    )
```

A callable converter signals an unknown value only by raising `ValueError`;
any other exception (including `KeyError`) is a bug and propagates, failing
the read.

## Bit fields

Single-bit fields decode to `bool | None`. Each carries its own space, so a
component may mix them freely.

- `coil` — a coil (FC01). Read/write; pass `writable=True` to allow writes.
- `discrete_input` — a discrete input (FC02). Read-only — it has no `writable`
  option because discrete inputs are physically read-only.

```python
coil(address, *, writable=False, stride=0) -> CoilField
discrete_input(address, *, stride=0) -> DiscreteInputField
```

```python
class IO(Component):
    relay = coil(0, writable=True)
    fault = discrete_input(0)  # distinct address space from coil 0
```

## Writable fields and validators

`writable=True` marks a field writable and writes the value as-is. Passing a
**validator callable** instead both marks it writable and vets the value before
each write — it receives the requested value and returns the value to actually
write, or raises to reject it, before anything reaches the device:

```python
def in_range(value: int) -> int:
    if not 0 <= value <= 100:
        raise ValueError(f"{value} out of range")
    return value


class Boiler(Component):
    setpoint = integer(0, writable=in_range)
```

For registers, `write()` picks FC06 for a single word and FC16 for multiple. Pass
`force_fc16=True` for a device that honours only FC16 even for one register.

## Dynamic scale factors

`scale_register` points at a separate register whose signed int16 value is read
alongside the field and applied as `10**sf` — the SunSpec `sunssf` convention.

A `write()` on a dynamically-scaled field takes the engineering value: the
scale factor is read fresh in the same write and the value is snapped to the
precision the factor grants before encoding, so `12.349` with a `10**-2`
factor writes raw `1235`. An exponent whose factor cannot scale — such as
SunSpec's not-implemented `sunssf` value — raises `ValueError`: a write never
guesses a scale, where a read decodes the same case to `None`.

A field may also declare `scale_exponent_range=(low, high)` for a spec that
bounds the exponent; a register-sourced exponent outside it decodes the value
to `None` and refuses writes the same way. The SunSpec point types declare the
`sunssf` spec range (-10..10) out of the box. See the
[SunSpec page](/modbus-connection/modelling/sunspec/) for the pre-wired point
types built on this.

## Field classes

The helpers return instances of a small set of codec classes —
`NumberField`, `FloatField`, `StringField`, `RawField`, and the address types
(`IPv4Field`, `IPv6Field`, `Eui48Field`) — plus `CoilField` / `DiscreteInputField`
for bits. Reach for a subclass directly only for a codec the helpers don't
cover; almost everything is expressible with the helpers above.
