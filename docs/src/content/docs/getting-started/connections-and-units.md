---
title: Connections and units
description: Understand connection ownership, lifecycle, and per-unit handles.
---

The top-level `modbus_connection` package defines the abstract
`ModbusConnection` and the `ModbusUnit` Protocol. It imports no backend.

## `ModbusConnection`

A connection owns one physical link to a Modbus network. One link can serve many
unit IDs, and all requests on it are serialized.

Constructing a connection performs no I/O. The first request connects on demand.
If the link drops, the next request reconnects. `connect()` remains available
for callers that specifically need to establish the link eagerly.

Only the connection owner should retain this object and call `close()`. Closing
is permanent: later calls to `connect()` or unit operations raise
`ClientClosedError`.

```python
from modbus_connection import ModbusTcpParams
from modbus_connection.tmodbus import ModbusConnection

connection = ModbusConnection(
    ModbusTcpParams(host="192.168.1.50", port=502)
)
```

## `ModbusUnit`

`connection.for_unit(unit_id)` returns a stateless handle bound to that unit.
Consumers should receive this handle rather than the owning connection.

```python
unit = connection.for_unit(1)
values = await unit.read_holding_registers(9, 2)
```

:::note[Legacy connection factories]
The backend modules retain `connect_tcp`, `connect_udp`, `connect_tls`, and
`connect_serial` temporarily for compatibility; they are scheduled for removal.
New code should construct the backend's `ModbusConnection` with a shared
parameter object.
:::

Continue with [Connection parameters](/modbus-connection/getting-started/connection-parameters/)
to configure a link or [Modbus operations](/modbus-connection/getting-started/operations/)
to use a unit.
