---
title: Modbus operations
description: Read and write registers and bits on a ModbusUnit, and convert the raw register words to and from Python values.
---

`ModbusUnit` exposes the standard Modbus operations. Register reads return
`list[int]`; bit reads return `list[bool]`.

```python
# Register I/O
await unit.read_holding_registers(address, count)  # FC03
await unit.read_input_registers(address, count)  # FC04
await unit.write_register(address, value)  # FC06
await unit.write_registers(address, values)  # FC16

# Coils and discrete inputs
await unit.read_coils(address, count)  # FC01
await unit.read_discrete_inputs(address, count)  # FC02
await unit.write_coil(address, value)  # FC05
await unit.write_coils(address, values)  # FC15
```

The interface also includes exception status (FC07), diagnostics (FC08),
comm-event counter and log (FC11/FC12), report-server-id (FC17), file records
(FC20/FC21), mask-write (FC22), read/write-registers (FC23), FIFO queue (FC24),
and device identification (FC43/14). The
[reference](/modbus-connection/connection/reference/#modbusunit) lists every
method with its signature and function code.

All operations raise on failure — see
[Exceptions](/modbus-connection/connection/reference/#exceptions) for the error
hierarchy.

## Decoding what you read

Register reads hand back raw 16-bit words. `modbus_connection.decode` converts
them to Python values:

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
