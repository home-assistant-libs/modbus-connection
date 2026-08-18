---
title: Built-in fields
description: Every generic field helper — integers, gauges, floats, strings, enums, flags, coils and discrete inputs — with its options.
---

A **field** is a descriptor you place on a `Component`. It owns the codec (how
raw register words become a Python value) but holds no per-read state. Reading
the attribute returns `T | None`: the decoded value, or `None` before the first
read or when a sentinel decodes to "no value".

Prefer the **helpers** below over constructing field classes directly. They are
named presets (width, sign, sentinel, scale) over a small set of codecs. This
page covers what each helper means and when to use it. The full signatures live
in the [field reference](/modbus-connection/modelling/fields-reference/). Every
helper lives in `modbus_connection.model`:

```python
from modbus_connection.model import (
    integer,
    gauge,
    boolean,
    bit,
    bits,
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

Most register helpers accept the same keyword arguments. Not every option
applies to every helper — a `string` has no `scale`. The
[field reference](/modbus-connection/modelling/fields-reference/#options-shared-by-the-register-helpers)
lists all of them. Three need more explanation than a table row.

### Affine scaling

Numeric fields decode as `raw * scale + offset`. Pass `offset` for a device that
reports a shifted value — e.g. `gauge(0, 0.1, offset=-100)` for a temperature
stored as `raw * 0.1 - 100`. Writable fields invert it as `(value - offset) / scale`.

### The `nan` sentinel

Many devices send a reserved value to mean "this point is not implemented". Pass
that raw value as `nan=` and the field decodes it to `None`:

```python
temperature = gauge(5, 0.1, nan=0x8000)  # 0x8000 -> None
```

A device may define several distinct "no value" codes for the same register —
one for an absent register, another for an unplugged probe. Pass all of them:

```python
temperature = gauge(5, 0.1, nan=(0x8000, 0xF448))  # either -> None
```

The values are matched against the **raw** register word, before `signed` is
applied. State them as they appear on the wire: `0xF448`, not `-3000`.

### Word order

`word_order` selects the order of the 16-bit registers in a multi-register value.
It defaults to `"big"` (the Modbus convention), covering the ABCD arrangement.
Pass `"little"` for CDAB.

---

## Numeric fields

### `integer`

An unscaled integer register — counts, percentages, addresses.

```python
count = integer(4)  # signed 16-bit int
percent = integer(7, signed=False)  # 0..65535
shifted = integer(2, offset=-100)  # raw - 100
```

### `gauge`

A scaled numeric register — a 0.1-scaled temperature, a voltage, and so on. The
one helper where `scale` is a **required positional** argument.

```python
voltage = gauge(0, 0.1, unit="V")  # raw * 0.1
temp = gauge(9, 0.1, offset=-40, unit="°C")  # raw * 0.1 - 40
```

### `raw_register`

A single raw register word — no scaling, sign handling, or sentinel. Useful for
a status word you decode yourself.

```python
status = raw_register(7)  # the word as-is, 0..65535
```

### 32- and 64-bit integers

`uint32` / `int32` span two consecutive registers; `uint64` / `int64` span four.
All take `scale`, `offset`, `word_order`, `unit`, and the write options.

```python
energy = uint32(2, unit="Wh")  # 32-bit over registers 2–3
signed_power = int32(10, word_order="little")  # CDAB word order
lifetime = uint64(20, unit="Wh")  # 64-bit over registers 20–23
```

## Floating-point fields

`float32` decodes an IEEE-754 single over two registers; `float64` a double over
four. Both take `scale`, `offset`, `word_order`, `unit`, and the write options.

```python
flow = float32(40, unit="m³/h")
precise = float64(50)
```

## String fields

`string` reads a fixed-length null-padded ASCII string over `length` registers
(two characters per register).

```python
serial = string(100, 8)  # 8 registers -> up to 16 ASCII characters
```

## Enum and flag fields

Map a raw register natively to an `IntEnum` or `IntFlag`.

- `enum` — an `IntEnum` field. A code with no member decodes to `None` (warned
  once per distinct value).
- `flags` — an `IntFlag` field. Unknown bits are **kept**.

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

`signed` interprets the code as two's-complement, for devices with negative
enum codes (e.g. `-1` sent as `0xFFFF`). The default is unsigned.

Under the hood both helpers pass the enum class to `NumberField(convert=...)`.
`convert` accepts any `Callable[[int], T]` — an enum class is a callable that
raises `ValueError` for unknown codes — or a `Mapping[int, T]`, where a missing
key means the same. Either way an unknown value decodes to `None`, warned once
per distinct value. For a mapping an enum class can't express (e.g. onto a
`StrEnum`), pass the dict inline:

```python
from enum import StrEnum

from modbus_connection.model import Component, NumberField


class State(StrEnum):
    OFF = "off"
    RUNNING = "running"


class Device(Component):
    state: NumberField[State] = NumberField(
        5, signed=False, convert={1: State.OFF, 4: State.RUNNING}
    )
```

A callable converter signals an unknown value only by raising `ValueError`.
Any other exception (including `KeyError`) is a bug and propagates, failing
the read.

## Boolean register fields

Many devices report on/off state as a 0/1 **register** rather than a coil.
`boolean` decodes such a register to `bool | None`. Any value other than 0 or 1
decodes to `None` (warned once), so an out-of-spec code reads as unknown rather
than truthy:

```python
class Relay(Component):
    output = boolean(0, writable=True)  # holding register: 0 = off, 1 = on
```

Pass `nan=` for a device with a "no value" sentinel; the sentinel decodes to
`None` without a warning. For an actual coil or discrete input, use the bit
fields below — `boolean` reads the component's register space.

## Packed bits in a register

Devices routinely pack several independent settings into one register. `bit`
exposes one of them as a `bool`, and `bits` a run of them as an `int`:

```python
class SiteLimit(Component):
    limit_mode = bits(0xE000, 0, 3, writable=True)  # bits 0-2
    external_production = bit(0xE000, 10, writable=True)
    negative_limit = bit(0xE000, 11, writable=True)
```

Fields at the same address are read together, so this costs one register.
Writing one of them re-reads the register, replaces that field's bits, and
writes the word back. Every other setting is left alone — including one changed
since the last poll, by the device itself or by another writer. A value too
wide for the run raises `ValueError` instead of being truncated.

## Bit fields

Single-bit fields decode to `bool | None`. Each carries its own space, so a
component may mix them freely.

- `coil` — a coil (FC01). Read/write; pass `writable=True` to allow writes.
- `discrete_input` — a discrete input (FC02). Read-only — it has no `writable`
  option because discrete inputs are physically read-only.

```python
class IO(Component):
    relay = coil(0, writable=True)
    fault = discrete_input(0)  # distinct address space from coil 0
```

## Writable fields and validators

`writable=True` marks a field writable and writes the value as-is. Passing a
**validator callable** instead both marks it writable and vets the value before
each write. The validator receives the requested value and returns the value to
actually write, or raises to reject it, before anything reaches the device:

```python
def in_range(value: int) -> int:
    if not 0 <= value <= 100:
        raise ValueError(f"{value} out of range")
    return value


class Boiler(Component):
    setpoint = integer(0, writable=in_range)
```

The library ships no validators of its own. For ready-made ones, see
[probatio](https://github.com/frenck/probatio).

For registers, `write()` picks FC06 for a single word and FC16 for multiple. Pass
`force_fc16=True` for a device that honours only FC16 even for one register.

## Dynamic scale factors

`scale_register` points at a separate register whose signed int16 value is read
alongside the field and applied as `10**sf` — the SunSpec `sunssf` convention.

A `write()` on a dynamically-scaled field takes the engineering value. The
scale factor is read fresh in the same write, and the value is snapped to the
precision the factor grants before encoding. For example, `12.349` with a
`10**-2` factor writes raw `1235`. An exponent whose factor cannot scale — such
as SunSpec's not-implemented `sunssf` value — raises `ValueError`: a write
never guesses a scale. A read decodes the same case to `None`.

A field may also declare `scale_exponent_range=(low, high)` for a spec that
bounds the exponent. A register-sourced exponent outside the range decodes the
value to `None` and refuses writes the same way. The SunSpec point types
declare the `sunssf` spec range (-10..10) out of the box. See the
[SunSpec page](/modbus-connection/modelling/sunspec/) for the pre-wired point
types built on this.

## When the helpers don't fit

Almost every device map is expressible with the helpers above. For the rest,
there are two ways out.

**Shape the value in a `@property`** — for composing or transforming several
fields, or for packed dates and times. Keep the field private and expose the
computed value, so static typing stays exact:

```python
from modbus_connection.model import Component, string


class Controller(Component):
    _firmware = string(10, 4)  # 4 registers of ASCII, e.g. "1.23"

    @property
    def model(self) -> str | None:
        firmware = self._firmware
        return f"TROVIS 5576 ({firmware})" if firmware is not None else None
```

**Construct a field class directly** when the codec itself is the problem — the
device packs its words in a way no helper decodes. The helpers on this page
return instances of a small set of classes: `NumberField`, `FloatField`,
`StringField`, `RawField`, `PackedBitField` / `PackedBitsField` for packed bits,
and `CoilField` / `DiscreteInputField` for bits of their own. The
[field reference](/modbus-connection/modelling/fields-reference/#field-classes)
documents each class's constructor and attributes.
