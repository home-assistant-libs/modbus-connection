---
title: Modbus operations
description: Use the raw register, bit, diagnostic, file-record, and identification operations on a ModbusUnit.
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
comm-event counter and log (FC0B/FC0C), report-server-id (FC11), file records
(FC14/FC15), mask-write (FC16), read/write-registers (FC17), FIFO queue (FC18),
and device identification (FC2B/0E).

All operations raise on failure. See [Exceptions](/modbus-connection/reference/exceptions/)
for the error hierarchy and [Encoding and decoding](/modbus-connection/getting-started/encoding-decoding/)
for converting register words.
