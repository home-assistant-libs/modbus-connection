---
title: Connections and units
description: Connection ownership, lifecycle, and per-unit handles — and how to configure the transport, TLS, and request spacing.
---

The top-level `modbus_connection` package defines the `ModbusConnection` and
`ModbusUnit` Protocols. It imports no backend.

## `ModbusConnection`

A connection owns one physical link to a Modbus network. One link can serve many
unit IDs, and all requests on it are serialized.

Constructing a connection performs no I/O. The first request connects on demand.
If the link drops, the next request reconnects. `connect()` remains available
for callers that specifically need to establish the link eagerly.

A link that is up but unresponsive — a peer that keeps the socket open but
stops answering, common with cheap serial-to-network bridges — never drops on
its own. Call `disconnect()` to recycle it: the link is torn down and the next
request establishes a fresh one, over the same unit handles and components.

Only the connection owner should retain this object and call `close()`. Closing
is permanent: later calls to `connect()` or unit operations raise
`ClientClosedError`.

```python
from modbus_connection import ModbusTcpParams
from modbus_connection.tmodbus import ModbusConnection

connection = ModbusConnection(ModbusTcpParams(host="192.168.1.50", port=502))
```

## `ModbusUnit`

`connection.for_unit(unit_id)` returns a stateless handle bound to that unit.
Consumers should receive this handle rather than the owning connection.

```python
unit = connection.for_unit(1)
values = await unit.read_holding_registers(9, 2)
```

## Choosing connection parameters

A connection is constructed from one of four frozen, keyword-only dataclasses,
importable from `modbus_connection` — `ModbusTcpParams`, `ModbusUdpParams`,
`ModbusSerialParams`, or `ModbusTlsParams`. Because the parameter object is
shared and backend-neutral, the code that gathers connection details (a config
flow, a CLI) doesn't need to know which backend will consume them. The
[reference](/modbus-connection/connection/reference/#parameter-dataclasses)
lists every field and default.

`framer` selects the wire framing. TCP and UDP accept `socket` (native Modbus),
`rtu`, or `ascii`; serial accepts `rtu` or `ascii`; TLS framing is fixed. Not
every backend carries every framing — see
[Choosing a backend](/modbus-connection/getting-started/backends/).

```python
from modbus_connection import ModbusSerialParams
from modbus_connection.tmodbus import ModbusConnection

connection = ModbusConnection(
    ModbusSerialParams(device="/dev/ttyUSB0", framer="ascii", baudrate=9600),
    timeout=5,
)
```

### Sharing one connection per device

Because the dataclasses are frozen and hashable, a params object can serve as
the identity key for a pool of shared connections. When consumers may describe
the same device with *different* link settings, key on
[`params.endpoint`](/modbus-connection/connection/reference/#endpoint) instead:
it identifies only the physical target (`host` and `port`, or the serial
`device` path), so two configs for `/dev/ttyUSB0` at different baud rates — or
the same host and port with different framing — map to the same key. Equal
endpoints with unequal params mean conflicting settings for one device, which a
pool should reject rather than open a second competing link.

### TLS

`ModbusTlsParams` verifies the server certificate against the system trust
store by default. Pass `verify=False` to disable verification (self-signed
devices), `verify="/path/to/ca"` to verify against a private CA,
`check_hostname=False` to skip only the hostname check, `client_cert` /
`client_key` / `client_key_password` for mutual TLS, or `sslctx` to supply a
ready-made `ssl.SSLContext` that overrides the other options.

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

## Connect delay

Some devices need a pause **after the link opens** before they answer reliably.
Set `connect_delay` in seconds on the connection; it is awaited each time the
link is established — the first connect and every reconnect — before any
request uses it:

```python
connection = ModbusConnection(
    ModbusTcpParams(host="192.168.1.50"),
    connect_delay=1.0,
)
```

This is not request pacing: `message_spacing` spaces requests on a live link,
while `connect_delay` runs once per connection establishment.

:::note[Legacy connection factories]
The backend modules retain `connect_tcp`, `connect_udp`, `connect_tls`, and
`connect_serial` for compatibility. They are no longer recommended; new code
should construct the backend's `ModbusConnection` with a shared parameter object.
:::

Continue with [Modbus operations](/modbus-connection/connection/operations/)
to use a unit.
