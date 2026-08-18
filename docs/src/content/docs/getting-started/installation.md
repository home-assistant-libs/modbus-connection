---
title: Installation
description: Install modbus-connection and verify the package is available.
---

modbus-connection requires **Python 3.12 or newer**.

The top-level package is a pure interface and imports no Modbus library. A bare
install therefore pulls **neither** backend. Pick one with an extra:

```bash
pip install "modbus-connection[tmodbus]"    # tmodbus backend
pip install "modbus-connection[pymodbus]"   # pymodbus backend
```

You can install both extras and choose the backend at runtime. The
`ModbusConnection` and `ModbusUnit` APIs are identical across them.

See [Choosing a backend](/modbus-connection/getting-started/backends/) before
selecting an extra.

## Verifying the install

```python
import modbus_connection

print(modbus_connection.__all__)
# ['ReadBlock', 'ServerDeviceFailureError', 'ServerDeviceBusyError', ...]
```

Importing `modbus_connection` never imports a backend. The backend loads only
when you import `modbus_connection.tmodbus` or `modbus_connection.pymodbus`.

Continue with the [Quickstart](/modbus-connection/getting-started/quickstart/)
to read your first registers.
