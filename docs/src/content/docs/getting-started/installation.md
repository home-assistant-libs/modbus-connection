---
title: Installation
description: Install modbus-connection and verify the package is available.
---

modbus-connection requires **Python 3.12 or newer**.

The top-level package is a pure interface and imports no Modbus library, so a
bare install pulls **neither** backend. Pick one with an extra:

```bash
pip install "modbus-connection[pymodbus]"   # pymodbus backend
pip install "modbus-connection[tmodbus]"    # tmodbus backend
```

You can install both extras if you want to choose the backend at runtime — the
`ModbusConnection` and `ModbusUnit` APIs are identical across them.

See [Choosing a backend](/modbus-connection/getting-started/backends/) before
selecting an extra.

## Verifying the install

```python
import modbus_connection

print(modbus_connection.__all__)
# ['ModbusConnection', 'ModbusConnectionError', 'ModbusError', ...]
```

Importing `modbus_connection` never imports a backend. The backend is only
loaded when you import `modbus_connection.pymodbus` or `modbus_connection.tmodbus`.
