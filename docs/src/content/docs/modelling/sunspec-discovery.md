---
title: SunSpec discovery
description: Scan a SunSpec model chain and place components at discovered model addresses.
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

Use [SunSpec generation](/modbus-connection/modelling/sunspec-generation/) to
create initial component classes from the official definitions.
