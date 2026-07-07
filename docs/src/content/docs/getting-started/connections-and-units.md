---
title: Connections and units
description: The ModbusConnection and ModbusUnit protocols, the connect functions, transports, TLS, and message spacing.
---

The top-level `modbus_connection` package is a **pure interface**. It defines two
[Protocols](https://typing.readthedocs.io/en/latest/spec/protocol.html) —
`ModbusConnection` and `ModbusUnit` — the shared `WordOrder` type, and a small
exception hierarchy. It imports no Modbus library, so you can type against it
without committing to a backend.

## The connection / unit split

The two classes have deliberately different roles.

### `ModbusConnection` — the owner's link

A `ModbusConnection` is a shared, internally-serialized, **already-connected**
link to a Modbus network. One physical link addresses many units (1–247), and
sharing a single connection across many consumers is strictly better than each
opening a competing socket.

- It is **transient and owner-held**. A backend *connect function* returns a
  live, already-connected instance — there is **no `connect()`** on the object.
- Requests are serialized per connection **by the backend**, not by this wrapper:
  pymodbus's transaction manager and tmodbus's smart transport each hold a lock
  for the full request/response cycle, so concurrent unit calls on one connection
  can't interleave.
- It does **not** self-reconnect. On a drop it fires `on_connection_lost`
  and stops; recreating it is the owner's job.
- **Only the owner holds it, and only the owner tears it down** with `close()`.

```python
class ModbusConnection(Protocol):
    @property
    def connected(self) -> bool: ...
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
conn = await connect_tcp("192.168.1.50", port=502)
unit = conn.for_unit(1)          # a ModbusUnit for station 1
temp = await unit.read_holding_registers(9, 1)
```

:::tip[Who holds what]
The owner keeps the `ModbusConnection` and is responsible for its lifetime.
Consumers only ever see a `ModbusUnit`. This keeps ownership unambiguous: nothing
downstream can accidentally close a link that other consumers are still using.
:::

## Connecting

Each backend ships one connect function per wire transport. They are `async` and
return a live `ModbusConnection`:

```python
import asyncio
from modbus_connection.pymodbus import connect_tcp
from modbus_connection.decode import decode_int16, decode_float32


async def main() -> None:
    conn = await connect_tcp("192.168.1.50", port=502)
    try:
        unit = conn.for_unit(1)
        outside_temp = decode_int16(await unit.read_holding_registers(9, 1))
        flow_setpoint = decode_float32(await unit.read_holding_registers(40, 2))
        pump_on = (await unit.read_coils(56, 1))[0]
        print(outside_temp, flow_setpoint, pump_on)
    finally:
        await conn.close()


asyncio.run(main())
```

Swapping to tmodbus is a one-line import change — the rest is identical:

```python
from modbus_connection.tmodbus import connect_tcp
```

## Transports

| Function | Transport | `framer` options |
| --- | --- | --- |
| `connect_tcp(host, *, port=502, framer="socket")` | Modbus TCP, or RTU-/ASCII-over-TCP (serial-to-Ethernet gateways) | `socket` / `rtu` / `ascii` |
| `connect_udp(host, *, port=502, framer="socket")` | Modbus UDP | `socket` / `rtu` / `ascii` |
| `connect_serial(port, *, framer="rtu", baudrate=…, bytesize=…, parity=…, stopbits=…)` | Modbus serial — RTU or ASCII | `rtu` / `ascii` |
| `connect_tls(host, *, port=802, verify=True, …)` | Modbus/TLS (Modbus Security) | — (always TLS framing) |

`framer` names the wire framing across every transport; its value set differs by
transport (`socket`/`rtu`/`ascii` for TCP/UDP, `rtu`/`ascii` for serial; TLS is
fixed).

```python
from modbus_connection.pymodbus import connect_udp, connect_serial, connect_tls

udp = await connect_udp("192.168.1.50", port=502)
ascii_serial = await connect_serial("/dev/ttyUSB0", framer="ascii", baudrate=9600)
tls = await connect_tls("device.local")  # verifies the server certificate
```

tmodbus exposes the same functions **except** `connect_udp` and
`connect_tcp(framer="ascii")` — it has no UDP or ASCII-over-TCP transport, so
those raise `NotImplementedError`. Use the pymodbus backend for them.

### TLS options

`connect_tls` verifies the server certificate against the system trust store by
default (`verify=True`) and checks the hostname (`check_hostname=True`):

- `verify=False` — a device with a self-signed certificate.
- `verify="/path/to/ca"` — a private CA (file or directory).
- `check_hostname=False` — verify the certificate but not the hostname.
- `client_cert` / `client_key` / `client_key_password` — present a client
  certificate for **mutual TLS**.
- `sslctx` — pass a ready-made `ssl.SSLContext` for full control.

## Message spacing

Some devices need a pause between frames. Pass `message_spacing` (seconds) to a
connect function and every request — from any unit sharing the link — waits until
that gap has elapsed since the previous one **finished**:

```python
conn = await connect_serial("/dev/ttyUSB0", message_spacing=0.1)
```

The package applies the gap itself, so it works the same across backends. It is
the spacing *between* requests only — to delay the *first* request, sleep before
issuing it. The default `0` disables it.

## The raw ModbusUnit surface

`ModbusUnit` exposes the full 19-function-code Modbus surface, not just the common
reads and writes:

```python
# Register I/O
await unit.read_holding_registers(address, count)   # FC03 -> list[int]
await unit.read_input_registers(address, count)     # FC04 -> list[int]
await unit.write_register(address, value)           # FC06
await unit.write_registers(address, values)         # FC16

# Coils / discrete inputs
await unit.read_coils(address, count)               # FC01 -> list[bool]
await unit.read_discrete_inputs(address, count)     # FC02 -> list[bool]
await unit.write_coil(address, value)               # FC05
await unit.write_coils(address, values)             # FC15
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

decode_int16(await unit.read_holding_registers(9, 1))     # signed 16-bit
decode_float32(await unit.read_holding_registers(40, 2))  # IEEE-754 over 2 regs
decode_string(await unit.read_holding_registers(10, 4))   # ASCII over 4 regs
```

For anything more than a handful of values, prefer the
[device-modelling framework](/modbus-connection/modelling/overview/): it wraps
these codecs in typed fields and pools the reads for you.

## Errors

Every backend maps its errors onto one neutral hierarchy, so `except ModbusError`
catches them all regardless of backend. See
[Exceptions](/modbus-connection/reference/exceptions/) for the full tree.
