---
title: Connections and units
description: The ModbusConnection class and ModbusUnit protocol, direct construction and connect factories, transports, and message spacing.
---

The top-level `modbus_connection` package is a **pure interface**. It defines
the `ModbusConnection` base class and the `ModbusUnit`
[Protocol](https://typing.readthedocs.io/en/latest/spec/protocol.html) to type
against, a small exception hierarchy, and the shared connection-params
dataclasses. It also re-exports the shared `WordOrder` datatype (used by
`decode` / `encode` and the `model` framework) for convenient importing — it is
not part of the connection surface. The package imports no Modbus library, so
you can type against it without committing to a backend.

## The connection / unit split

The two classes have deliberately different roles.

### `ModbusConnection` — the owner's link

A `ModbusConnection` is a shared, internally-serialized link to a Modbus network.
One physical link addresses many units (1–247), and sharing a single connection
across many consumers is strictly better than each opening a competing socket.

- It is **owner-held**, and there is **no `connect()`** on the object. The
  connection [connects on demand](#connecting): on its first request, or up
  front through a `connect_*` factory — and reconnects on demand after a drop
  (retrying an interrupted read-only request once; writes are never replayed).
- Requests are serialized per connection **by the backend**, not by this wrapper:
  the backend's transport holds a lock for the full request/response cycle, so
  concurrent unit calls on one connection can't interleave.
- **Only the owner holds it, and only the owner tears it down** with `close()`.
  After `close()` any request raises `ClientClosedError`.

The surface owners and consumers see (type against
`modbus_connection.ModbusConnection`, the abstract base both backends subclass):

```python
class ModbusConnection:
    @property
    def connected(self) -> bool: ...
    async def connect(self) -> None: ...  # optional: requests do it on demand
    def for_unit(self, unit_id: int) -> ModbusUnit: ...
    def on_connection_lost(self, callback): ...
    async def close(self) -> None: ...  # owner only
```

### `ModbusUnit` — the consumer's handle

A `ModbusUnit` is a **stateless handle bound to one unit ID** on a shared
connection. You obtain it with `connection.for_unit(unit_id)` and hand it to a
consumer (a device library, a `Component`, …).

- It holds no buffered state beyond the address.
- It has **no lifecycle methods**: a consumer cannot connect or close the link it
  rides on.
- Every method **raises** on any failure (timeout, exception response, link down).
  It never returns `None` or swallows errors.

```python
conn = ModbusConnection(ModbusTcpParams(host="192.168.1.50", port=502))
unit = conn.for_unit(1)  # a ModbusUnit for station 1
temp = await unit.read_holding_registers(9, 1)  # first request connects
```

:::tip[Who holds what]
The owner keeps the `ModbusConnection` and is responsible for its lifetime.
Consumers only ever see a `ModbusUnit`. This keeps ownership unambiguous: nothing
downstream can accidentally close a link that other consumers are still using.
:::

## Connecting

Each backend exports a concrete **`ModbusConnection`** — picking a backend is
picking an import:

```python
from modbus_connection.tmodbus import ModbusConnection  # tmodbus-backed
from modbus_connection.pymodbus import ModbusConnection  # pymodbus-backed
```

There are two equivalent entry points; both yield the same self-healing
connection, built from a frozen parameters dataclass — `ModbusTcpParams`,
`ModbusUdpParams`, `ModbusTlsParams`, or `ModbusSerialParams`.

**Direct construction** does **no I/O**: the first request connects, and the
connection reconnects on demand. A read-only request interrupted by a transport
drop is retried once on a fresh connection. Writes, read/write operations, and
diagnostics are never replayed because a lost response leaves their outcome
unknown. This suits long-lived owners that should stay up while a device sleeps
— a failed poll is just a failed poll. To establish eagerly, call
`await conn.connect()` yourself — it is a no-op when already connected, and it
is exactly what every request runs on demand.

```python
import asyncio
from modbus_connection import ModbusTcpParams
from modbus_connection.tmodbus import ModbusConnection
from modbus_connection.decode import decode_int16, decode_float32


async def main() -> None:
    conn = ModbusConnection(ModbusTcpParams(host="192.168.1.50", port=502))
    try:
        unit = conn.for_unit(1)
        # The first read is what connects; a sleeping device just fails its polls.
        outside_temp = decode_int16(await unit.read_holding_registers(9, 1))
        flow_setpoint = decode_float32(await unit.read_holding_registers(40, 2))
        pump_on = (await unit.read_coils(56, 1))[0]
        print(outside_temp, flow_setpoint, pump_on)
    finally:
        await conn.close()


asyncio.run(main())
```

**The connect factories** — `connect_tcp` / `connect_udp` / `connect_tls` /
`connect_serial` on each backend module — build the params and call
`connect()` before returning, so an unreachable device raises
`ModbusConnectionError` right at the call. Afterwards the returned connection
self-heals exactly like a directly-constructed one.

```python
from modbus_connection.tmodbus import connect_tcp

conn = await connect_tcp("192.168.1.50", port=502)  # raises if unreachable
print(conn.connected)  # True — already live
```

See [Which backend?](/modbus-connection/getting-started/installation/#which-backend)
for the transport matrix.

### Parameters

The params dataclasses are shared and backend-neutral. One dataclass per
transport, all
frozen and keyword-only, so an instance doubles as a connection identity key:

| Dataclass | Fields (with defaults) |
| --- | --- |
| `ModbusTcpParams` | `host`, `port=502`, `framer="socket"` (`"socket"` / `"rtu"` / `"ascii"`) |
| `ModbusUdpParams` | `host`, `port=502`, `framer="socket"` (`"socket"` / `"rtu"` / `"ascii"`) |
| `ModbusTlsParams` | `host`, `port=802`, `verify=True`, `check_hostname=True`, `client_cert=None`, `client_key=None`, `client_key_password=None`, `sslctx=None` |
| `ModbusSerialParams` | `device`, `baudrate=9600`, `bytesize=8`, `parity="N"`, `stopbits=1`, `framer="rtu"` (`"rtu"` or `"ascii"`) |

`ModbusConnection` also takes keyword-only `timeout` (default `3`) and
`message_spacing` (see [below](#message-spacing)).

TCP's `framer` selects native Modbus TCP (MBAP), RTU-over-TCP — the framing a
transparent serial-to-Ethernet gateway speaks — or ASCII frames tunnelled over
the TCP stream. UDP is connectionless: "connecting" only binds the local
endpoint, so a dead peer surfaces as a timeout on the first read.

### TLS options

`ModbusTlsParams` verifies the server certificate against the system trust store
by default (`verify=True`) and checks the hostname (`check_hostname=True`):

- `verify=False` — a device with a self-signed certificate.
- `verify="/path/to/ca"` — a private CA (file or directory).
- `check_hostname=False` — verify the certificate but not the hostname.
- `client_cert` / `client_key` / `client_key_password` — present a client
  certificate for **mutual TLS**.
- `sslctx` — use a ready-made `ssl.SSLContext` as-is. It overrides the other TLS
  options and can be shared by multiple connections.

Without `sslctx`, the certificate context is built on the first connect (in a
thread), so direct construction stays free of I/O.

## Message spacing

Some devices need a pause between frames. Pass `message_spacing` (seconds) to
`ModbusConnection` and every request — from any unit
sharing the link — waits until that gap has elapsed since the previous one
**finished**:

```python
conn = ModbusConnection(ModbusSerialParams(device="/dev/ttyUSB0"), message_spacing=0.1)
```

The package applies the gap itself, so it works the same across backends. It is
the spacing *between* requests only — to delay the *first* request, sleep before
issuing it. The default `0` disables it.

### Per-unit spacing

`message_spacing` paces the *whole link*. When only one slow device on a shared
connection needs the pause — and forcing it on every unit would needlessly slow
the rest — set the gap on that unit instead:

```python
slow = conn.for_unit(7)
slow.set_message_spacing(0.05)  # ≥50 ms between requests to unit 7
```

It layers on top of any connection-wide `message_spacing`: a request to the unit
waits for whichever gap is longer. The gap belongs to the **unit id**, not the
handle — it applies to every request to that unit, including through other
handles handed out for the same id (as when a
[connection owner](/modbus-connection/home-assistant/integration/) lends units to
consumers). Pass `0` to clear it.

## The raw ModbusUnit surface

`ModbusUnit` exposes the full 19-function-code Modbus surface, not just the common
reads and writes:

```python
# Register I/O
await unit.read_holding_registers(address, count)  # FC03 -> list[int]
await unit.read_input_registers(address, count)  # FC04 -> list[int]
await unit.write_register(address, value)  # FC06
await unit.write_registers(address, values)  # FC16

# Coils / discrete inputs
await unit.read_coils(address, count)  # FC01 -> list[bool]
await unit.read_discrete_inputs(address, count)  # FC02 -> list[bool]
await unit.write_coil(address, value)  # FC05
await unit.write_coils(address, values)  # FC15
```

Beyond these it also exposes the diagnostic and identification codes — exception
status (0x07), diagnostics (0x08), comm-event counter/log (0x0B / 0x0C),
report-server-id (0x11), mask-write (0x16), read/write-registers (0x17), FIFO
queue (0x18), file records (0x14 / 0x15), and device identification (0x2B/0x0E).
A backend that cannot implement a code raises `NotImplementedError` (tmodbus does,
for diagnostics and the comm-event codes).

The raw reads return lists of `int` (registers) or `bool` (bits) — no datatype
decoding. That lives one layer up.

## Decoding and encoding

`modbus_connection.decode` and `.encode` turn register words into Python values
and back. They are what the raw reads above feed into:

```python
from modbus_connection.decode import decode_int16, decode_float32, decode_string

decode_int16(await unit.read_holding_registers(9, 1))  # signed 16-bit
decode_float32(await unit.read_holding_registers(40, 2))  # IEEE-754 over 2 regs
decode_string(await unit.read_holding_registers(10, 4))  # ASCII over 4 regs
```

For anything more than a handful of values, prefer the
[device-modelling framework](/modbus-connection/modelling/overview/): it wraps
these codecs in typed fields and pools the reads for you.

## Errors

Every backend maps its errors onto one neutral hierarchy, so `except ModbusError`
catches them all regardless of backend. See
[Exceptions](/modbus-connection/reference/exceptions/) for the full tree.
