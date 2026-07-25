---
title: SunSpec generation
description: Generate initial component classes from official SunSpec model definitions.
---

SunSpec publishes its standard model definitions as JSON in
[sunspec/models](https://github.com/sunspec/models). Generate component classes
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

Place the generated classes with
[SunSpec discovery](/modbus-connection/modelling/sunspec-discovery/).
