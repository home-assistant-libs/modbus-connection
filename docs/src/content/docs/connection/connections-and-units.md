---
title: Connections and units
description: The two classes — an owner-held connection and the per-unit handles it hands out — and how to configure the link.
---

The top-level `modbus_connection` package defines the abstract
`ModbusConnection` and the `ModbusUnit` Protocol. It imports no backend.

## `ModbusConnection`

One physical link to a Modbus network, shared by every unit id on it. Requests
are serialized over that link, so two units never interleave frames.

Constructing a connection performs no I/O. Pick a backend and hand it a
[parameter object](#connection-parameters):

```python
from modbus_connection import ModbusTcpParams
from modbus_connection.tmodbus import ModbusConnection

connection = ModbusConnection(ModbusTcpParams(host="192.168.1.50", port=502))
```

The first request connects on demand. If the link drops, the next request
reconnects. Call `connect()` only when you need to establish the link eagerly.

Some links stay up but stop responding. A peer can keep the socket open and
stop answering; cheap serial-to-network bridges often do this. Such a link
never drops on its own. Call `disconnect()` to recycle it: the link is torn
down, and the next request establishes a fresh one. Unit handles and components
keep working across the recycle.

Only the connection owner should retain this object and call `close()`. Closing
is permanent: later calls to `connect()` or unit operations raise
`ClientClosedError`.

## `ModbusUnit`

One device on that link. `connection.for_unit(unit_id)` returns a handle that
carries every read and write operation for that unit id. See
[Modbus operations](/modbus-connection/connection/operations/) for the full set.

```python
unit = connection.for_unit(1)
values = await unit.read_holding_registers(9, 2)
```

The handle is stateless and cheap, so call `for_unit` whenever you need a unit.
Give consumers a handle, not the owning connection. A consumer with a handle
can talk to its own unit, and can `disconnect()` a wedged link, but cannot
close the connection out from under the owner.

## Connection parameters

A connection is constructed from one of four frozen, keyword-only dataclasses,
importable from `modbus_connection`. The parameter object is shared and
backend-neutral. Code that gathers connection details (a config flow, a CLI)
does not need to know which backend will consume them:

```python
from modbus_connection import (
    ModbusSerialParams,
    ModbusTcpParams,
    ModbusTlsParams,
    ModbusUdpParams,
)

ModbusTcpParams(host="192.168.1.50", port=502)  # native Modbus TCP
ModbusTcpParams(host="192.168.1.50", framer="rtu")  # RTU over TCP
ModbusUdpParams(host="192.168.1.50", port=502)
ModbusSerialParams(device="/dev/ttyUSB0", framer="ascii", baudrate=9600)
ModbusTlsParams(host="192.168.1.50", port=802, verify="/path/to/ca.pem")
```

`framer` selects the wire framing. TCP and UDP accept `socket` (native Modbus),
`rtu`, or `ascii`. Serial accepts `rtu` or `ascii`. TLS framing is fixed. Not
every backend carries every framing — see
[Choosing a backend](/modbus-connection/getting-started/backends/).

The [reference](/modbus-connection/connection/reference/#parameter-dataclasses)
lists every field and default. `timeout`, `message_spacing` and `connect_delay`
belong to the connection rather than the parameters. Pass them to
`ModbusConnection` itself.

### TLS

`ModbusTlsParams` verifies the server certificate against the system trust
store by default. The options:

- `verify=False` disables verification (self-signed devices).
- `verify="/path/to/ca"` verifies against a private CA.
- `check_hostname=False` skips only the hostname check.
- `client_cert` / `client_key` / `client_key_password` enable mutual TLS.
- `sslctx` supplies a ready-made `ssl.SSLContext` that overrides the other
  options.

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
Set `connect_delay` in seconds on the connection. The delay is awaited each time
the link is established — the first connect and every reconnect — before any
request uses it:

```python
connection = ModbusConnection(
    ModbusTcpParams(host="192.168.1.50"),
    connect_delay=1.0,
)
```

This is not request pacing. `message_spacing` spaces requests on a live link;
`connect_delay` runs once per connection establishment.

:::note[Legacy connection factories]
The backend modules retain `connect_tcp`, `connect_udp`, `connect_tls`, and
`connect_serial` for compatibility. They are no longer recommended. New code
should construct the backend's `ModbusConnection` with a shared parameter
object.
:::

Continue with [Modbus operations](/modbus-connection/connection/operations/)
to use a unit.
