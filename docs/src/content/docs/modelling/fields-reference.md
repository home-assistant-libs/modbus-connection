---
title: Field reference
description: Every field helper and field class of modbus_connection.model — the generic helpers, the codec classes, the writable/convert types, and the SunSpec point helpers.
---

The complete API of the modelling layer's fields. The generic helpers and
classes are importable from `modbus_connection.model`; the
[SunSpec point helpers](#sunspec-point-helpers) from
`modbus_connection.model.sunspec`. The components the fields are declared on
are in the [component reference](/modbus-connection/modelling/components-reference/).

## Field helpers

Factory functions that build the [field classes](#field-classes) as named
presets. Declare them as `Component` class attributes; reading the attribute
returns the decoded value or `None`. See
[Built-in fields](/modbus-connection/modelling/fields/) for the guide.

| Helper | Returns | Registers | Notes |
| --- | --- | --- | --- |
| `integer(address, *, offset=0.0, signed=True, nan=None, …)` | `NumberField[int]` | 1 | An unscaled integer. |
| `gauge(address, scale, *, offset=0.0, signed=True, nan=None, …)` | `NumberField[float]` | 1 | A scaled number; `scale` is required. |
| `raw_register(address, *, stride=0, writable=False, force_fc16=False)` | `RawField` | 1 | A raw word — no scaling, sign handling, or sentinel. |
| `uint32(address, …)` / `int32(address, …)` | `NumberField[int]` | 2 | 32-bit integers; take `scale`, `offset`, `word_order`. |
| `uint64(address, …)` / `int64(address, …)` | `NumberField[int]` | 4 | 64-bit integers. |
| `float32(address, …)` / `float64(address, …)` | `FloatField` | 2 / 4 | IEEE-754 floats; take `scale`, `offset`, `word_order`. |
| `string(address, length, *, stride=0, writable=False, force_fc16=False)` | `StringField` | `length` | Null-padded ASCII, two characters per register. |
| `enum(address, enum_type, *, count=1, signed=False, nan=None, …)` | `NumberField[E]` | `count` | Maps to an `IntEnum`; unknown codes decode to `None`. |
| `flags(address, flag_type, *, count=1, signed=False, nan=None, …)` | `NumberField[F]` | `count` | Maps to an `IntFlag`; unknown bits are kept. |
| `coil(address, *, writable=False, stride=0)` | `CoilField` | 1 bit | A coil (FC01). |
| `discrete_input(address, *, stride=0)` | `DiscreteInputField` | 1 bit | A discrete input (FC02, read-only). |

### Options shared by the register helpers

| Option | Meaning |
| --- | --- |
| `address` | Address of the value's first register word, in declared coordinates. |
| `scale` / `offset` | Affine transform: the value decodes as `raw * scale + offset`. |
| `signed` | Interpret the raw integer as two's-complement. |
| `nan` | Raw sentinel value — an `int` or an iterable of them — that decodes to `None`. |
| `word_order` | `"big"` (default) or `"little"` for multi-register values. |
| `unit` | Unit-of-measure label carried as metadata; not used in decoding. |
| `stride` | Per-index address step for a [repeated sub-unit](/modbus-connection/modelling/repeats/). |
| `writable` | `False` (default), `True`, or a [`WriteValidator`](#writevalidator). |
| `force_fc16` | Always write with FC16, even a single register. Raises `ValueError` without `writable`. |
| `scale_register` | Address of a scale-factor register whose signed int16 value scales the field as `10**sf`. |
| `scale_register_stride` | Per-index address step for `scale_register`. |

### `repeating_group(count, component_class, *, stride)`

Create a `RepeatingGroupField` describing repeated sub-components. `count` is a
fixed `int` (must be `>= 0`; instances fold into the normal read) or a
`RegisterField[int]` read at poll time (a second read pass sizes the list).
`stride` is the block length (must be `> 0`, or `ValueError`). Reading the
attribute returns `list[C]` — the instances built on the last update. See
[Repeated sub-units](/modbus-connection/modelling/repeats/).

## Field classes

The classes the helpers return. Construct one directly only for something the
helpers can't express (e.g. a `convert` mapping); subclass `RegisterField` for a
custom codec.

### `RegisterField[T]`

The abstract base of every register field, generic over the decoded type.

```python
RegisterField(address, *, count=1, writable=False, stride=0, unit=None,
              scale_register=None, scale_register_stride=0, force_fc16=False)
```

Instance attributes: `address`, `count`, `writable`, `stride`, `unit`,
`scale_register`, `scale_register_stride`, `force_fc16`, and `name` (set to the
attribute name when declared on a class). As a descriptor, reading it on an
instance returns `T | None`; on the class, the field object itself.

- **`decode(words, scale_exponent=None)`** — decode register words into the
  field's value; `scale_exponent` is the signed int16 read from
  `scale_register`, if any. Implemented by each subclass.
- **`encode(value, scale_exponent=None)`** — encode a value into register
  words. The base implementation raises `NotImplementedError` (read-only
  codec); numeric, raw, float, and string fields implement it.

### `NumberField[T]`

`RegisterField` subclass decoding a scaled or mapped integer. Adds constructor
options `signed=True`, `convert=None` ([`Converter`](#converter)),
`enum_type=None` (shorthand for `convert`; passing both raises `ValueError`),
`word_order="big"`, `scale=1.0`, `offset=0.0`, `nan=None`, and
`scale_exponent_range=None` (a `(low, high)` bound on a register-sourced
exponent; outside it the value decodes to `None` and writes raise
`ValueError`). The `nan` sentinel is matched against the raw unsigned value
before `signed` or `convert` apply. Results are rounded to the decimals implied
by `scale` and `offset`.

### `RawField`

`RegisterField[int]` decoding the raw register words as an unsigned integer —
no scaling, sign handling, or sentinel. Takes `word_order="big"`.

### `FloatField`

IEEE-754 float over two (`count=2`) or four (`count=4`) registers. Takes
`word_order`, `scale`, `offset`, and `nan` (any non-`None` value makes NaN
decode to `None`).

### `StringField`

Fixed-length null-padded ASCII string over `count` registers, two characters
per word.

### `IPv4Field`, `IPv6Field`, `Eui48Field`

Read-only address codecs: an `ipaddress.IPv4Address` over two registers, an
`ipaddress.IPv6Address` over eight, and a colon-separated EUI-48 / MAC string
over three.

### `CoilField` and `DiscreteInputField`

Bit fields, constructed by [`coil()`](#field-helpers) and
[`discrete_input()`](#field-helpers).

```python
CoilField(address, *, writable=False, stride=0)
DiscreteInputField(address, *, stride=0)
```

Instance attributes: `address`, `stride`, `writable` (always `False` on a
`DiscreteInputField`), `name`, `space` (`"coil"` / `"discrete"`), and `count`
(always `1`). Reading the attribute on a component returns `bool | None`;
`decode(words)` returns the single bit as a `bool`.

### `RepeatingGroupField[C]`

The descriptor [`repeating_group()`](#repeating_groupcount-component_class--stride)
returns. Instance attributes: `count` (an `int` or `RegisterField[int]`),
`component_class`, `stride`, and `name`. Reading it on a component instance
returns `list[C]`.

## Supporting types

### `WriteValidator`

`Callable[[Any], Any]` — a callable passed as a field's `writable`. It marks
the field writable and is invoked with the requested value before encoding,
returning the value to actually write; raise to reject.

### `Converter`

`Callable[[int], Any] | Mapping[int, Any]` — a `NumberField`'s `convert`:
maps the decoded (sign-applied) integer to the field's value. A callable
raising `ValueError`, or a mapping missing the key, decodes to `None` (warned
once per distinct value); any other exception propagates.

## SunSpec point helpers

From `modbus_connection.model.sunspec` — see
[SunSpec fields](/modbus-connection/modelling/sunspec/) for the guide, and the
[component reference](/modbus-connection/modelling/components-reference/#sunspec-discovery-and-components)
for `scan` and `SunSpecComponent`.

Each numeric helper is a preset over [`NumberField`](#numberfieldt) with the
SunSpec "unimplemented" sentinel baked in (decoding to `None`) and the
`sunssf` spec range (-10..10) declared as its `scale_exponent_range`. Unless
noted, they take `scale=1.0`, `scale_register=None`, `scale_register_stride=0`,
`stride=0`, `writable=False`, and `unit=None`.

| Helper | Returns | Registers | Sentinel |
| --- | --- | --- | --- |
| `int16(address, …)` | `NumberField[float]` | 1 | `0x8000` |
| `uint16(address, …)` | `NumberField[float]` | 1 | `0xFFFF` |
| `int32(address, …)` | `NumberField[float]` | 2 | `0x80000000` |
| `uint32(address, …)` | `NumberField[float]` | 2 | `0xFFFFFFFF` |
| `int64(address, …)` | `NumberField[float]` | 4 | `0x8000_0000_0000_0000` |
| `uint64(address, …)` | `NumberField[float]` | 4 | `0xFFFF_FFFF_FFFF_FFFF` |
| `acc16 / acc32 / acc64(address, …)` | `NumberField[int]` | 1 / 2 / 4 | `0` ("not accumulated"); no `writable` option |
| `sunssf(address, *, stride=0)` | `NumberField[int]` | 1 | `0x8000` |
| `enum16 / enum32(address, enum=None, *, stride=0, writable=False)` | `NumberField[E]` or `NumberField[int]` | 1 / 2 | `0xFFFF` / `0xFFFFFFFF` |
| `bitfield16 / bitfield32 / bitfield64(address, flags=None, *, stride=0, writable=False)` | `NumberField[F]` or `NumberField[int]` | 1 / 2 / 4 | `0xFFFF` / `0xFFFFFFFF` / `0xFFFF…` |
| `float32 / float64(address, *, stride=0, writable=False, unit=None)` | `FloatField` | 2 / 4 | any NaN |
| `string(address, length, *, stride=0, writable=False)` | `StringField` | `length` | — |
| `ipaddr(address, *, stride=0)` | `IPv4Field` | 2 | — |
| `ipv6addr(address, *, stride=0)` | `IPv6Field` | 8 | — |
| `eui48(address, *, stride=0)` | `Eui48Field` | 3 | — |
