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
| ASCII-over-TCP | ✅ | ✅ |
| Native Modbus UDP | ✅ | ✅ |
| RTU-/ASCII-over-UDP | ❌ | ✅ |
| Serial RTU and ASCII | ✅ | ✅ |
| Modbus/TLS | ✅ | ✅ |
| ESPHome `serial_proxy` target | ✅ | ❌ |
| Distinguishes a corrupt reply from no reply | ✅ | ❌ |
| Raises `ModbusDesyncError` for a reply to a different exchange | ✅ | ❌ |
| Busy-device response | Retried, then raised | Raised immediately |

The full operation list, with each method's signature and function code, is in
the [`ModbusUnit` reference](/modbus-connection/connection/reference/#modbusunit).

tmodbus can identify a corrupt frame and raises
[`ModbusProtocolError`](/modbus-connection/connection/reference/#modbusprotocolerror)
for it. pymodbus maps both a corrupt reply and a missing reply to
[`ModbusTimeoutError`](/modbus-connection/connection/reference/#modbustimeouterror).

tmodbus retries `SERVER_DEVICE_BUSY` responses with exponential backoff for up
to one minute. This retry is native to tmodbus; this library only bounds and
configures it. pymodbus has no equivalent layer, so it raises the busy response
immediately. Neither backend retries timeouts, dropped links, or other
exception responses.

Constructing a connection with unsupported parameters raises `ValueError`.
