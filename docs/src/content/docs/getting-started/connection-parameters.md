---
title: Connection parameters
description: Configure transports, TLS, timeouts, and request spacing.
---

Connection parameter objects are frozen and keyword-only. Import them from
`modbus_connection` and pass one to the concrete connection class:

| Dataclass | Transport | Key fields |
| --- | --- | --- |
| `ModbusTcpParams` | Modbus TCP, or RTU-/ASCII-over-TCP | `host`, `port=502`, `framer="socket"` |
| `ModbusUdpParams` | Modbus UDP | `host`, `port=502`, `framer="socket"` |
| `ModbusSerialParams` | Modbus serial | `device`, `framer="rtu"`, `baudrate=9600`, `bytesize=8`, `parity="N"`, `stopbits=1` |
| `ModbusTlsParams` | Modbus/TLS | `host`, `port=802`, certificate options |

`framer` selects the wire framing. TCP and UDP accept `socket`, `rtu`, or
`ascii`; serial accepts `rtu` or `ascii`; TLS framing is fixed.

```python
from modbus_connection import ModbusSerialParams
from modbus_connection.pymodbus import ModbusConnection

connection = ModbusConnection(
    ModbusSerialParams(device="/dev/ttyUSB0", framer="ascii", baudrate=9600),
    timeout=5,
)
await connection.connect()
```

## TLS

`ModbusTlsParams` verifies the server certificate against the system trust store
by default.

| Option | Purpose |
| --- | --- |
| `verify=False` | Disable certificate verification. |
| `verify="/path/to/ca"` | Verify against a private CA file or directory. |
| `check_hostname=False` | Verify the certificate without checking its hostname. |
| `client_cert`, `client_key`, `client_key_password` | Present a client certificate. |
| `sslctx` | Use an existing `ssl.SSLContext` and ignore the other TLS options. |

## Request spacing

Some devices require a pause between frames. Set `message_spacing` in seconds on
the connection:

```python
connection = ModbusConnection(
    ModbusSerialParams(device="/dev/ttyUSB0"),
    message_spacing=0.1,
)
```

The interval is measured from the completion of one request to the start of the
next. The default `0` disables spacing.

To pace only one device on a shared link, set the interval on its unit:

```python
connection.for_unit(7).set_message_spacing(0.05)
```

This setting belongs to the unit ID and applies to every handle for that ID. It
combines with connection-wide spacing by waiting for the longer interval. Pass
`0` to clear it.
