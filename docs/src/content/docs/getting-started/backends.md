---
title: Choosing a backend
description: Compare tmodbus and pymodbus support and select the backend for a connection.
---

Both backend modules export their concrete connection class as
`ModbusConnection`. Selecting a backend changes the import, not the API:

```python
from modbus_connection.tmodbus import ModbusConnection
# or: from modbus_connection.pymodbus import ModbusConnection
```

Code that accepts a `ModbusUnit` does not depend on either implementation.

| Capability | tmodbus | pymodbus |
| --- | --- | --- |
| Native Modbus TCP | ✅ | ✅ |
| RTU-over-TCP | ✅ | ✅ |
| ASCII-over-TCP | ❌ | ✅ |
| UDP | ❌ | ✅ |
| Serial RTU and ASCII | ✅ | ✅ |
| Modbus/TLS | ✅ | ✅ |
| ESPHome `serial_proxy` target | ✅ | ❌ |
| Distinguishes a corrupt reply from no reply | ✅ | ❌ |
| Busy-device response | Retried, then raised | Raised immediately |
| Diagnostics (FC08) and comm-event codes (FC0B/FC0C) | ❌ | ✅ |

tmodbus can identify a corrupt frame and raises `ModbusProtocolError`.
pymodbus maps both a corrupt reply and a missing reply to `ModbusTimeoutError`.

tmodbus retries `SERVER_DEVICE_BUSY` responses with exponential backoff for up
to one minute. pymodbus raises the response immediately. Neither backend retries
timeouts, dropped links, or other exception responses.

Constructing a connection with unsupported parameters raises `ValueError`. A
`ModbusUnit` operation that the selected backend does not implement raises
`NotImplementedError`.
