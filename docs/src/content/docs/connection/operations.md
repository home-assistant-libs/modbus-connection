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
comm-event counter and log (FC11/FC12), report-server-id (FC17), file records
(FC20/FC21), mask-write (FC22), read/write-registers (FC23), FIFO queue (FC24),
and device identification (FC43/14). The
[reference](/modbus-connection/connection/reference/#modbusunit) lists every
method with its signature and function code.

All operations raise on failure. See
[Exceptions](/modbus-connection/connection/reference/#exceptions) for the error
hierarchy and [Encoding and decoding](/modbus-connection/connection/encoding-decoding/)
for converting register words.
