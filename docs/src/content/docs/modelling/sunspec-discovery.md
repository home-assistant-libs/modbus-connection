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

models = await scan(unit, 40000)  # dict[int, list[SunSpecModel]]
```

`base_address` is the zero-based marker address. SunSpec defines 0, 40000, and
50000 as possible locations. Each `SunSpecModel` contains the model ID, header
address, and data length. A model ID can occur more than once.

## Components at discovered models

Subclass `SunSpecComponent`, declare fields relative to the model header, and
construct it with a discovered model:

```python
from modbus_connection.model.sunspec import SunSpecComponent, sunssf, uint16


class Inverter(SunSpecComponent):
    a = uint16(2, scale_register=6)
    a_sf = sunssf(6)


if (found := models.get(103)) is not None:
    inverter = Inverter(unit, found[0])
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
names come from the model name, with the model ID added when names collide.

```python
class OperatingState(IntEnum):
    OFF = 1
    SLEEPING = 2


class InverterThreePhase(SunSpecComponent):
    """Represent SunSpec model 103."""

    a = uint16(2, scale_register=6, unit="A")
    st = enum16(38, OperatingState)
```

Layouts whose addresses or strides depend on values read from the device cannot
always be emitted statically. The generator leaves those repeated-group
declarations commented for the library author to complete. It raises
`SunSpecGenerationError` when emitting a static layout would be incorrect.
