---
title: Device modelling reference
description: Every class, method, field, and helper of modbus_connection.model — Component, ComponentGroup, ManualComponent, the field classes, and the SunSpec module.
---

The complete API of the device-modelling layer. Everything in the first
sections is importable from `modbus_connection.model`; the
[SunSpec section](#sunspec-modbus_connectionmodelsunspec) covers
`modbus_connection.model.sunspec`.

## `Component`

Maps a device sub-system to typed register and bit attributes. Subclass it and
declare [fields](#field-helpers) as class attributes — see the
[overview](/modbus-connection/modelling/overview/) for the full guide.

```python
Component(unit, index=1, *, base_offset=0)
```

| Parameter | Type | Meaning |
| --- | --- | --- |
| `unit` | `ModbusUnit` | The unit handle to read from and write to. |
| `index` | `int`, default `1` | 1-based instance index for a layout with per-field `stride` — see [Repeated sub-units](/modbus-connection/modelling/repeats/). |
| `base_offset` | `int`, default `0` | Shift applied to every address the component touches, placing the whole declared layout at another base address. |

### Class attributes

All are overridable on a subclass (or set per instance).

| Attribute | Type | Default | Meaning |
| --- | --- | --- | --- |
| `register_space` | `"holding" \| "input"` | `"holding"` | The register space this component's register fields are read from (FC03 / FC04). |
| `register_ranges` | `tuple[Range, ...] \| None` | `None` | The device's readable address ranges in the component's register space; `None` falls back to gap-based planning. Stated in declared coordinates, so they shift with `base_offset`. |
| `coil_ranges` | `tuple[Range, ...] \| None` | `None` | Readable ranges in the coil space (FC01). |
| `discrete_ranges` | `tuple[Range, ...] \| None` | `None` | Readable ranges in the discrete-input space (FC02). |
| `max_gap` | `int` | `16` | Gap-based planning only: spans within this many addresses merge into one read. |
| `max_span` | `int` | `125` | The widest a single block read may be (125 is the Modbus per-request ceiling). |
| `scale_in_block` | `bool` | `False` | On a repeating sub-unit: shift `scale_register` addresses with each instance instead of keeping them in the parent's fixed block. |
| `declared_fields` | `Mapping[str, RegisterField \| bit field]` | — | Read-only mapping of attribute name to declared field object, in declaration order; available on the class and its instances, and never narrowed by `restrict_fields`. |

### Methods

#### `async_update()`

`async` — read every field with pooled block reads, decode the values, and
notify the listeners. The read plan is built and cached on the first call.
Raises [`BlockReadError`](/modbus-connection/connection/reference/#blockreaderror)
if the device rejects a block; the update then applies nothing.

#### `async_update_repeating_groups()`

`async` — resize and update only the register-counted
[`repeating_group`](#repeating_group) fields (their counts must already have
been read). `async_update()` does this as its second pass; call it directly only
to refresh the groups alone.

#### `async_read_raw()`

`async` — run the same reads as `async_update()` (refreshing the fields and
firing listeners) and additionally return the raw words and bits as
`{space: {address: value}}`, keyed by the four Modbus spaces (`"holding"`,
`"input"`, `"coil"`, `"discrete"`) with addresses ascending. Raises
`BlockReadError` if the device rejects a block.

#### `write(field, value)`

`async` — write a writable register or coil by attribute name. A register write
uses FC06 for a single word and FC16 for multiple (or always FC16 with
[`force_fc16`](#options-shared-by-the-register-helpers)); a coil write uses
FC05. A [validator](#writevalidator) set as `writable` vets the value first,
and a dynamically-scaled field reads its scale factor fresh in the same write.
Raises `AttributeError` for an unknown or read-only field (input registers and
discrete inputs are always read-only) and `ValueError` if the value cannot be
scaled.

#### `restrict_fields(names)`

Narrow this component to the fields in `names` and reshape its read plan so no
block spans an excluded field's registers. Excluded fields read as `None` and
can no longer be written. Raises `ValueError` for an unknown field name, or if
the component declares a `repeating_group` (not supported). See
[Restricting fields](/modbus-connection/modelling/restricting-fields/).

#### `add_update_listener(listener)`

Register a `Callable[[], None]` fired after each update; returns an unsubscribe
callable.

#### `notify()`

Fire this component's update listeners, and each repeating-group instance's.
`async_update()` calls it for you.

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

## `ComponentGroup`

Pools reads for several components on one unit — see
[Component groups](/modbus-connection/modelling/component-group/).

```python
ComponentGroup(unit, components)
```

| Parameter | Type | Meaning |
| --- | --- | --- |
| `unit` | `ModbusUnit` | The unit every component is read from. |
| `components` | `Iterable[Component]` | The members. Their resolved readable ranges must agree per space (a member that constrains a space cannot pool with one that leaves it open), and all must share `max_gap` and `max_span` — a conflict raises `ValueError`. |

### Methods

#### `async_update(*, notify=True)`

`async` — refresh every member with pooled block reads, then size and refresh
each member's register-counted repeating groups. Fires each member's listeners
unless `notify=False`. Raises
[`BlockReadError`](/modbus-connection/connection/reference/#blockreaderror) if
the device rejects any block.

#### `async_read_raw()`

`async` — like `Component.async_read_raw()`, merged across the members: the
pooled reads run, members refresh and notify, and the raw
`{space: {address: value}}` map comes back.

#### `notify()`

Fire each member component's update listeners.

## `ManualComponent`

A component whose layout is built at runtime instead of declared on a class —
see [Manual components](/modbus-connection/modelling/manual-component/).
Addresses are absolute (no `index` / `stride` / `base_offset`).

```python
ManualComponent(unit, *, max_gap=16, max_span=125, holding_ranges=None,
                input_ranges=None, coil_ranges=None, discrete_ranges=None)
```

| Parameter | Type | Meaning |
| --- | --- | --- |
| `unit` | `ModbusUnit` | The unit to read from and write to. |
| `max_gap` / `max_span` | `int` | The [planning limits](#class-attributes), per instance. |
| `holding_ranges` / `input_ranges` / `coil_ranges` / `discrete_ranges` | `tuple[Range, ...] \| None` | Readable ranges per table; a table left `None` falls back to gap-based planning. |

### Methods

#### `add(key, target, *, space=None)`

Add a read target under `key`, replacing any existing one and invalidating the
cached plan. `target` is a `RegisterField`, a bit field (`coil` /
`discrete_input`), or a `repeating_group`. `space` selects `"holding"`
(default) or `"input"` for a register target; passing it for a bit field or a
`repeating_group` raises `ValueError`, as does an unknown space. An unsupported
target type raises `TypeError`.

#### `remove(key)`

Remove the target under `key` (and any value read for it); invalidates the
cached plan. Removing an unknown key is a no-op.

#### `get(key)`

The value decoded for `key` on the last update — `None` if not yet read. For a
`repeating_group` key, the `list` of instances.

#### `values`

Property — a copy of all decoded values from the last update as
`dict[str, Any]` (repeating-group instances not included).

#### `async_update()`

`async` — read every target with pooled reads and return the decoded values as
a `dict`. Raises
[`BlockReadError`](/modbus-connection/connection/reference/#blockreaderror) if
the device rejects a block.

#### `write(key, value)`

`async` — write a writable register or coil by key, with the same behaviour
and errors as [`Component.write`](#writefield-value) (`AttributeError` for an
unknown or read-only key).

#### Shared with `Component`

`async_read_raw()`, `async_update_repeating_groups()`,
`add_update_listener(listener)`, and `notify()` work exactly as on
[`Component`](#methods).

## Supporting types

### `Range`

`tuple[int, int]` — an inclusive `(low, high)` readable address range.

### `RegisterSpace`

`Literal["input", "holding"]` — which register space a field is read from
(FC04 / FC03).

### `BitSpace`

`Literal["coil", "discrete"]` — which bit space a bit field is read from
(FC01 / FC02).

### `UpdateListener`

`Callable[[], None]` — the callback type `add_update_listener` takes.

### `WriteValidator`

`Callable[[Any], Any]` — a callable passed as a field's `writable`. It marks
the field writable and is invoked with the requested value before encoding,
returning the value to actually write; raise to reject.

### `Converter`

`Callable[[int], Any] | Mapping[int, Any]` — a `NumberField`'s `convert`:
maps the decoded (sign-applied) integer to the field's value. A callable
raising `ValueError`, or a mapping missing the key, decodes to `None` (warned
once per distinct value); any other exception propagates.

## SunSpec: `modbus_connection.model.sunspec`

SunSpec point types and model discovery — see
[SunSpec fields](/modbus-connection/modelling/sunspec/) and
[SunSpec discovery](/modbus-connection/modelling/sunspec-discovery/).

### Point helpers

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

### `scan(unit, base_address)`

`async` — walk the SunSpec model chain starting at the `"SunS"` marker at
`base_address` and return the discovered models as
`dict[int, list[SunSpecModel]]`, keyed by model ID (an ID can occur more than
once). Raises [`SunSpecError`](#sunspecerror) if the marker is absent or the
chain does not terminate within 100 models.

### `SunSpecModel`

A frozen dataclass locating one discovered model:

| Field | Type | Meaning |
| --- | --- | --- |
| `model_id` | `int` | The SunSpec model ID. |
| `address` | `int` | The address of the model's two-register header. |
| `length` | `int` | The data length in registers (excluding the header). |

### `SunSpecComponent`

A `Component` subclass placed at a discovered model's address. Declares two
fields of its own — `model_id` at offset 0 and `model_length` at offset 1, the
model header — and subclasses declare their points at header-relative offsets
(data starts at offset 2).

```python
SunSpecComponent(unit, model)
```

`model` is the [`SunSpecModel`](#sunspecmodel) from a scan; it becomes the
component's `base_offset`. `notify()` verifies the read-back header against the
discovered model after every update and raises
[`SunSpecMapShiftError`](#sunspecmapshifterror) on a mismatch before any
listener fires.

### Exceptions

#### `SunSpecError`

Raised when a device does not behave like a SunSpec device. Subclasses
`Exception`, not `ModbusError`.

#### `SunSpecMapShiftError`

`SunSpecError` subclass: a `SunSpecComponent`'s model header no longer matches
its discovered location — the register map has changed. Rescan and rebuild the
components.

#### `SunSpecGenerationError`

Raised by the [generator](/modbus-connection/modelling/sunspec-generation/)
(`modbus_connection.model.sunspec.generate`) when emitting a static layout
would be incorrect.
