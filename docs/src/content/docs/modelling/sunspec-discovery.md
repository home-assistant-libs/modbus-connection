---
title: Discovery and generation
description: Scan a SunSpec model chain, place components at discovered model addresses, and generate initial component classes from the official definitions.
---

A SunSpec device advertises its models after a `"SunS"` marker. Each model has a
two-register header containing its ID and data length. Model ID `0xFFFF`
terminates the chain.

`scan` returns each model ID and its locations:

```python
from modbus_connection.model.sunspec import scan

models = await scan(unit, 40000)  # SunSpecModels: dict[int, list[SunSpecModel]]
```

`base_address` is the zero-based marker address. SunSpec defines 0, 40000, and
50000 as possible locations. Each `SunSpecModel` contains the model ID, header
address, and data length. A model ID can occur more than once.

## Looking up models

The result is a `SunSpecModels` — a plain `dict` keyed by model ID, with three
lookups on top:

```python
models.first(103, 101)  # the first ID present, in preference order, or None
models.chain  # every model in chain order
models.at(40188)  # the model whose header sits there, or None
```

`chain` is what tells repeats of one ID apart: a SolarEdge meter is identified
by the model `1` immediately before it, not by its own ID.

A model's `length` is the data length its header reports. `span` adds the two
header registers, so it is both the count that reads the whole block and the
step to the next header.

## Components at discovered models

Subclass `SunSpecComponent`, declare fields relative to the model header, and
construct it with a discovered model:

```python
from modbus_connection.model.sunspec import SunSpecComponent, sunssf, uint16


class Inverter(SunSpecComponent):
    a = uint16(2, scale_register=6)
    a_sf = sunssf(6)


# a three-phase inverter if the device has one, else the single-phase model
if (found := models.first(103, 101)) is not None:
    inverter = Inverter(unit, found)
```

The header occupies offsets 0 and 1; data begins at offset 2.
`SunSpecComponent` verifies the header after every update. If the device moves a
model, it raises `SunSpecMapShiftError`; scan again and construct new components.

## Generating component classes

You don't have to write those component classes by hand. SunSpec publishes its
standard model definitions as JSON in
[sunspec/models](https://github.com/sunspec/models); generate component classes
from model IDs or local `model_N.json` files:

```bash
python -m modbus_connection.model.sunspec.generate 1 103 160 -o sunspec_models.py
```

Without `-o`, the module writes the generated source to standard output. The
result is ordinary source intended as a starting point. Review it against the
manufacturer's implementation and commit the adjusted classes to the device
library.

The output contains a `SunSpecComponent` subclass for each model, fields for its
points, enum and flag types, and statically expressible repeated groups. Class
names come from the model's group name, with the model ID added when names
collide, and each point's label and description become its attribute docstring.

```python
class OperatingState(IntEnum):
    OFF = 1
    SLEEPING = 2


class InverterThreePhase(SunSpecComponent):
    """SunSpec model 103: Inverter (Three Phase)."""

    a = uint16(2, scale_register=6, unit="A")
    """Amps. AC Current."""

    st = enum16(38, OperatingState)
    """Operating State."""
```

Layouts whose addresses or strides depend on values read from the device cannot
always be emitted statically — but they are only unknown because a count point
is. Pass what the target device reports and the block it sizes becomes an
ordinary fixed-count `repeating_group`, which is what makes the curve models
(705, 706, 712) and the trip models (707–710) generate at all:

```bash
python -m modbus_connection.model.sunspec.generate 705 707 \
    --count 705:NCrv=3 --count 705:NPt=4 \
    --count 707:NCrvSet=2 --count 707:NPt=5
```

Without a count the declaration is left commented, naming the option that would
emit it, and `SunSpecGenerationError` is raised when a static layout would be
incorrect. Counts are baked into the generated classes, so a device reporting
different ones fails on its first read rather than decoding garbage: a curve
model's length is a function of its counts, and `SunSpecComponent` verifies that
header.

## Writing a curve

A curve is a block of writable points repeated `NPt` times, and a device expects
it whole. Written a field at a time it costs a request per point, plus a read of
the scale register before each scaled write.

When a model has a repeated block whose points are **all** writable, the
generator gives the block's owner a method that writes it, over a `write_block`
helper emitted into the same module:

```python
class DERVoltVarCrv(Component):
    pt = repeating_group(4, DERVoltVarCrvPt, stride=2)

    async def write_pt(self, values: Sequence[Mapping[str, Any]]) -> None:
        """Write consecutive 'Pt' instances in one request.

        Each mapping sets one instance and must set every field:
        v, var. Instances past ``values`` are untouched.
        """
        await write_block(self, "pt", values)
```

Call it with one mapping per point, setting every field of that point:

```python
await volt_var.crv[1].write_pt(
    [
        {"v": 92.0, "var": 30.0},
        {"v": 98.0, "var": 0.0},
        {"v": 102.0, "var": 0.0},
        {"v": 108.0, "var": -30.0},
    ]
)
```

The method is named after its block rather than given one fixed name, because a
class can own several: model 704's controls block owns four, and gets
`write_pfw_inj`, `write_pfw_inj_rvrt`, `write_pfw_abs` and `write_pfw_abs_rvrt`.

The whole curve goes out as one FC16, and each distinct scale register is read
once for the block rather than once per field — one write and two reads instead
of eight of each. Points past the values given keep what they held.

Both the method and the helper it calls are generated source like the rest of
the module, not library API, so adjust them with the classes they serve. The
helper is emitted once per module however many models or blocks need it, and a
method appears only on blocks the generator has already checked are writable
throughout — a block with a read-only point or a nested block of its own gets
neither. Across the IEEE 1547 models that is every curve and trip-point block
(705, 706, 707–710, 712) and 704's power-factor blocks, but nothing on 711's
control block or 714's port block.

Write into a *stored* curve — one whose `read_only` point reports read-write
access — then adopt it with `adpt_crv_req`. That, not the register write itself,
is what makes a curve take effect.
