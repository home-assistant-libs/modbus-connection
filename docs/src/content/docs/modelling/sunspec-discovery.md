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

The result is a `SunSpecModels`: a plain `dict` keyed by model ID, with three
lookups on top:

```python
models.first(103, 101)  # the first ID present, in preference order, or None
models.chain  # every model in chain order
models.at(40188)  # the model whose header sits there, or None
```

`chain` tells repeats of one ID apart. For example, a SolarEdge meter is
identified by the model `1` immediately before it, not by its own ID.

A model's `length` is the data length its header reports. `span` adds the two
header registers. `span` is therefore both the count that reads the whole block
and the step to the next header.

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
`SunSpecComponent` verifies the header after every update. If the device moves
a model, it raises `SunSpecMapShiftError`. Scan again and construct new
components.

## Generating component classes

You don't have to write those component classes by hand. SunSpec publishes its
standard model definitions as JSON in
[sunspec/models](https://github.com/sunspec/models). Generate component classes
from model IDs or local `model_N.json` files:

```bash
python -m modbus_connection.model.sunspec.generate 1 103 160 -o sunspec_models.py
```

Without `-o`, the module writes the generated source to standard output. The
result is ordinary source intended as a starting point. Review it against the
manufacturer's implementation and commit the adjusted classes to the device
library.

The output contains a `SunSpecComponent` subclass for each model, fields for
its points, enum and flag types, and statically expressible repeated groups.
Class names come from the model's group name, with the model ID added when
names collide. Each point's label and description become its attribute
docstring.

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

A nested block whose count point sits in an outer block has no static
address or stride. Model 705's `Pt` sits inside `Crv` but is counted by `NPt`
in the fixed block. Pass the value your device reports with `--count`, and the
generator emits the block as a fixed-count `repeating_group`. The curve models
(705, 706, 712) and the trip models (707–710) need this:

```bash
python -m modbus_connection.model.sunspec.generate 705 707 \
    --count 705:NCrv=3 --count 705:NPt=4 \
    --count 707:NCrvSet=2 --count 707:NPt=5
```

Without a count, the generator leaves the declaration as a comment that names
the `--count` option to pass. It raises `SunSpecGenerationError` when a
device-sized block is not the last block, because the blocks after it have no
known address. The counts are baked into the generated classes. A device that
reports different counts has a different model length, and `SunSpecComponent`
rejects that header on the first read.

## Writing a curve

A curve is a repeated block of writable points, and a device expects it whole.
When every point of a repeated block is writable, the generator adds a method
to the block's owner that writes the block in one request:

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

Pass one mapping per point. Each mapping must set every field, because the
block goes to the device as one run of registers:

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

The registers go out as one FC16, and each distinct scale register is read
once. This four-point curve costs one write and two reads. The method is named
after its block because one class can own several: model 704's controls block
gets `write_pfw_inj`, `write_pfw_inj_rvrt`, `write_pfw_abs` and
`write_pfw_abs_rvrt`.

The `write_block` helper is emitted once into the generated module. It is
generated source, not library API. Adjust it with the classes that call it.

A block gets no method when it contains a nested block, a read-only point, or a
point that is never written, such as a scale factor or an accumulator.

Writing the registers does not activate a curve. Write into a curve whose
`read_only` point reports read-write access, then request it with
`adpt_crv_req`.
