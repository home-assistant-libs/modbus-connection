---
title: Quickstart
description: Read your first registers — connect, read, decode, and close in twenty lines.
---

With [an install](/modbus-connection/getting-started/installation/) done and a
backend picked, this is the whole loop — connect to a device, read two holding
registers, decode them, and close:

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

A few things worth noticing:

- **Constructing the connection performs no I/O** — the first read opens the
  link, and a dropped link heals on the next request. `close()` is the only
  teardown, and it is permanent.
- **The unit handle is where the operations live.** `for_unit(1)` selects unit
  1 on the shared link; consumers of your code should receive a `ModbusUnit`,
  never the connection.
- **Register reads return raw 16-bit words.** The `decode` module turns them
  into Python values — here two words into one unsigned 32-bit integer.
- Swapping `modbus_connection.tmodbus` for `modbus_connection.pymodbus` is the
  entire backend switch.

Every operation raises a subclass of `ModbusError` on failure, so a minimal
robust read is:

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
- [Device modelling](/modbus-connection/modelling/overview/) — for a device
  with more than a handful of values, map registers to typed attributes
  instead of decoding by hand.
