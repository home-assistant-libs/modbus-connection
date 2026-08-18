---
title: Quickstart
description: Read your first registers — connect, read, decode, and close in twenty lines.
---

This is the whole loop: connect to a device, read two holding registers, decode
them, and close. It assumes you have
[installed the package](/modbus-connection/getting-started/installation/) with a
backend extra.

```python
import asyncio

from modbus_connection import ModbusTcpParams
from modbus_connection.decode import decode_uint32
from modbus_connection.tmodbus import ModbusConnection


async def main() -> None:
    connection = ModbusConnection(ModbusTcpParams(host="192.168.1.50"))
    try:
        unit = connection.for_unit(1)
        words = await unit.read_holding_registers(2, 2)  # -> [word, word]
        print("raw words:", words)
        print("as uint32:", decode_uint32(words))
    finally:
        await connection.close()


asyncio.run(main())
```

Register reads return raw 16-bit words. The `decode` module turns them into
Python values — here, two words into one unsigned 32-bit integer. To switch
backends, replace `modbus_connection.tmodbus` with
`modbus_connection.pymodbus`. Nothing else changes.

Every operation raises a subclass of `ModbusError` on failure. A minimal robust
read looks like this:

```python
from modbus_connection import ModbusError

try:
    words = await unit.read_holding_registers(2, 2)
except ModbusError as err:
    print(f"read failed: {err}")
```

From here:

- [Connections and units](/modbus-connection/connection/connections-and-units/)
  — ownership, lifecycle, transports, and request spacing.
- [Modbus operations](/modbus-connection/connection/operations/) — the full
  operation surface and decoding.
- [Device modelling](/modbus-connection/modelling/overview/) — map registers to
  typed attributes instead of decoding by hand. Use this for any device with
  more than a handful of values.
