---
title: Component reference
description: Every class and method of the modelling layer's components — Component, ComponentGroup, ManualComponent, the supporting types, and SunSpec discovery.
---

The complete API of the modelling layer's component classes, importable from
`modbus_connection.model` (the [SunSpec section](#sunspec-discovery-and-components)
from `modbus_connection.model.sunspec`). The fields declared on them are in the
[field reference](/modbus-connection/modelling/fields-reference/).

## `Component`

Maps a device sub-system to typed register and bit attributes. Subclass it and
declare [fields](/modbus-connection/modelling/fields-reference/#field-helpers)
as class attributes — see the [overview](/modbus-connection/modelling/overview/)
for the full guide.

```python
Component(unit, index=1, *, base_offset=0)
```

| Parameter | Type | Meaning |
| --- | --- | --- |
| `unit` | `ModbusUnit` | The unit handle to read from and write to. |
| `index` | `int`, default `1` | 1-based instance index for a layout with per-field `stride` — see [Placing a component](/modbus-connection/modelling/placement/). |
| `base_offset` | `int`, default `0` | Shift applied to every address the component touches, placing the whole declared layout at another base address. |

### Class attributes

The configuration attributes are overridable on a subclass (or set per instance);
`declared_fields` and `resolved_fields` are derived from the declarations and
read-only.

| Attribute | Type | Default | Meaning |
| --- | --- | --- | --- |
| `register_space` | `"holding" \| "input"` | `"holding"` | The register space this component's register fields are read from (FC03 / FC04). |
| `register_ranges` | `tuple[Range, ...] \| None` | `None` | The addresses the device answers in the component's register space: a read may merge freely inside a range and never crosses a boundary. `None` falls back to gap-based planning. Stated in declared coordinates, so they shift with `base_offset`. |
| `coil_ranges` | `tuple[Range, ...] \| None` | `None` | Readable ranges in the coil space (FC01). |
| `discrete_ranges` | `tuple[Range, ...] \| None` | `None` | Readable ranges in the discrete-input space (FC02). |
| `max_gap` | `int` | `16` | Gap-based planning only: spans within this many addresses merge into one read. |
| `max_span` | `int` | `125` | The widest a single block read may be (125 is the Modbus per-request ceiling). |
| `scale_in_block` | `bool` | `False` | On a repeating sub-unit: shift `scale_register` addresses with each instance instead of keeping them in the parent's fixed block. |
| `declared_fields` | `Mapping[str, RegisterField \| CoilField \| DiscreteInputField]` | — | Read-only mapping of attribute name to declared field object, in declaration order; available on the class and its instances, and never narrowed by `restrict_fields`. |
| `resolved_fields` | `Mapping[str, ResolvedField]` | — | Read-only mapping of attribute name to [where the field sits on the device](#resolvedfield), in declaration order. Per instance, so it carries a repeated sub-unit's shift, and narrowed by `restrict_fields` to what the component reads. |

### Methods

#### `async_update(*, notify=True)`

`async` — read every field with pooled block reads, decode the values, and
notify the listeners. Pass `notify=False` for a caller that notifies them
itself. The read plan is built and cached on the first call. If the device
rejects a block, this raises the
[typed exception](/modbus-connection/connection/reference/#modbusexceptionerror)
for the code, with the refused block on `.block`, and the update applies
nothing.

#### `async_update_repeating_groups()`

`async` — resize and update only the register-counted
[`repeating_group`](/modbus-connection/modelling/fields-reference/#repeating_groupcount-component_class--stride)
fields (their counts must already have been read). `async_update()` does this as
its second pass; call it directly only to refresh the groups alone.

#### `async_read_raw(*, notify=True)`

`async` — run the same reads as `async_update()`, refreshing the fields and
firing listeners, and additionally return the raw words and bits as
`{space: {address: value}}`. The result is keyed by the four Modbus spaces
(`"holding"`, `"input"`, `"coil"`, `"discrete"`), addresses ascending. Raises
the typed exception if the device rejects a block, like `async_update()`.
`notify=False` skips the listeners; the fields still refresh.

#### `write(field, value)`

`async` — write a writable register or coil by attribute name. A register write
uses FC06 for a single word and FC16 for multiple (or always FC16 with
[`force_fc16`](/modbus-connection/modelling/fields-reference/#options-shared-by-the-register-helpers));
a coil write uses FC05. A
[validator](/modbus-connection/modelling/fields-reference/#writevalidator) set
as `writable` vets the value first, and a dynamically-scaled field reads its
scale factor fresh in the same write. Raises `AttributeError` for an unknown or
read-only field (input registers and discrete inputs are always read-only) and
`ValueError` if the value cannot be scaled.

#### `modbus_unit`

The [`ModbusUnit`](/modbus-connection/connection/reference/#modbusunit) this
component reads from and writes to. Also set on the sub-instances a
`repeating_group` builds.

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

## `ComponentGroup`

Pools reads for several components on one unit — see
[Component groups](/modbus-connection/modelling/component-group/).

```python
ComponentGroup(unit, components)
```

| Parameter | Type | Meaning |
| --- | --- | --- |
| `unit` | `ModbusUnit` | The unit every component is read from. |
| `components` | `Iterable[Component]` | The members. Their resolved readable ranges must agree per space where they overlap, and all must share `max_span` — a conflict raises `ValueError`. |

### Methods

#### `async_update(*, notify=True)`

`async` — refresh every member with pooled block reads, then size and refresh
each member's register-counted repeating groups. Fires each member's listeners
unless `notify=False`. Raises
the [typed exception](/modbus-connection/connection/reference/#modbusexceptionerror) if
the device rejects any block.

#### `async_read_raw(*, notify=True)`

`async` — like `Component.async_read_raw()`, merged across the members: the
pooled reads run, members refresh and notify, and the raw
`{space: {address: value}}` map comes back. `notify=False` skips the members'
listeners.

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
cached plan. `target` is a
[`RegisterField`](/modbus-connection/modelling/fields-reference/#registerfieldt),
a bit field (`coil` / `discrete_input`), or a `repeating_group`. `space`
selects `"holding"` (default) or `"input"` for a register target; passing it
for a bit field or a `repeating_group` raises `ValueError`, as does an unknown
space. An unsupported target type raises `TypeError`.

#### `remove(key)`

Remove the target under `key` (and any value read for it); invalidates the
cached plan. Removing an unknown key is a no-op.

#### `get(key)`

The value decoded for `key` on the last update — `None` if not yet read. For a
`repeating_group` key, the `list` of instances.

#### `values`

Property — a copy of all decoded values from the last update as
`dict[str, Any]` (repeating-group instances not included).

#### `async_update(*, notify=True)`

`async` — read every target with pooled reads and return the decoded values as
a `dict`; `notify=False` skips the listeners, for a caller that notifies them
itself. Raises
the [typed exception](/modbus-connection/connection/reference/#modbusexceptionerror) if
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

### `ResolvedField`

A frozen dataclass locating one field, returned in
[`resolved_fields`](#class-attributes):

| Field | Type | Meaning |
| --- | --- | --- |
| `field` | `RegisterField \| CoilField \| DiscreteInputField` | The declared field object, carrying `encode()`, `writable` and the rest. |
| `address` | `int` | Absolute address of its first register or bit. |
| `count` | `int` | Registers it spans; always `1` for a bit. |
| `scale_address` | `int \| None` | Absolute address of its scale register, or `None`. |
| `space` | `RegisterSpace \| BitSpace` | The space it is read from and written to. |

### `RegisterSpace`

`Literal["input", "holding"]` — which register space a field is read from
(FC04 / FC03).

### `BitSpace`

`Literal["coil", "discrete"]` — which bit space a bit field is read from
(FC01 / FC02).

### `UpdateListener`

`Callable[[], None]` — the callback type `add_update_listener` takes.

## SunSpec discovery and components

From `modbus_connection.model.sunspec` — see
[SunSpec discovery](/modbus-connection/modelling/sunspec-discovery/) for the
guide and the
[field reference](/modbus-connection/modelling/fields-reference/#sunspec-point-helpers)
for the point helpers.

### `scan(unit, base_address)`

`async` — walk the SunSpec model chain starting at the `"SunS"` marker at
`base_address` and return the discovered models as a
[`SunSpecModels`](#sunspecmodels), keyed by model ID (an ID can occur more
than once). Raises [`SunSpecError`](#sunspecerror) if the marker is absent or
the chain does not terminate within 100 models.

### `SunSpecModels`

The scan result: a `dict[int, list[SunSpecModel]]` subclass, usable as a
plain dict, with lookup helpers on top.

- **`first(*model_ids)`** — the first discovered [`SunSpecModel`](#sunspecmodel)
  among `model_ids`. The IDs are tried in the order given, so earlier IDs take
  priority (preferred model variants before their fallbacks). For an ID
  discovered more than once, the first location in chain order is returned.
  Returns `None` when no ID matches.
- **`chain`** — every discovered model in chain order, as a
  `list[SunSpecModel]` ascending by address. For a device that repeats a model
  ID, this is what distinguishes the repeats.
- **`at(address)`** — the model whose **header** sits at `address`, or `None`.
  An address inside a model's block is not a match.

### `SunSpecModel`

A frozen dataclass locating one discovered model:

| Field | Type | Meaning |
| --- | --- | --- |
| `model_id` | `int` | The SunSpec model ID. |
| `address` | `int` | The address of the model's two-register header. |
| `length` | `int` | The data length in registers, as the header reports it — excluding the header. |
| `span` | `int` | The registers the whole block occupies (`length + 2`): the count that reads the model, and the step to the next header. |

### `SunSpecComponent`

A `Component` subclass placed at a discovered model's address. Declares two
fields of its own — `model_id` at offset 0 and `model_length` at offset 1, the
model header — and subclasses declare their points at header-relative offsets
(data starts at offset 2).

```python
SunSpecComponent(unit, model)
```

`model` is the [`SunSpecModel`](#sunspecmodel) from a scan; it becomes the
component's `base_offset`. Every read verifies the read-back header against
the discovered model and raises
[`SunSpecMapShiftError`](#sunspecmapshifterror) on a mismatch.

`restrict_fields(names)` keeps `model_id` and `model_length` whether or not
`names` lists them, since the header is what that verification reads.

### Exceptions

#### `SunSpecError`

Raised when a device does not behave like a SunSpec device. Subclasses
`Exception`, not `ModbusError`.

#### `SunSpecMapShiftError`

`SunSpecError` subclass: a `SunSpecComponent`'s model header no longer matches
its discovered location — the register map has changed. Rescan and rebuild the
components.

#### `SunSpecGenerationError`

Raised by the
[generator](/modbus-connection/modelling/sunspec-discovery/#generating-component-classes)
(`modbus_connection.model.sunspec.generate`) when emitting a static layout
would be incorrect.
