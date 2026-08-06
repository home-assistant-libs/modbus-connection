---
title: Encoding and decoding
description: Convert between Modbus register words and Python values.
---

`modbus_connection.decode` converts register words to Python values:

```python
from modbus_connection.decode import decode_float32, decode_int16, decode_string

decode_int16(await unit.read_holding_registers(9, 1))
decode_float32(await unit.read_holding_registers(40, 2))
decode_string(await unit.read_holding_registers(10, 4))
```

`modbus_connection.encode` performs the inverse conversion for writes. Both
modules support signed and unsigned integers, floats, and strings, with
configurable word order for multi-register values; `decode` additionally covers
network addresses (IPv4, IPv6, EUI-48). The
[reference](/modbus-connection/connection/reference/#encoding-and-decoding-functions)
lists every function.

For a device with more than a handful of values, use the
[device-modelling framework](/modbus-connection/modelling/overview/) to attach
these conversions to fields and pool reads.
