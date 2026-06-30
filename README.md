# modbus-connection

A small, **backend-neutral** Modbus connection abstraction.

The top-level `modbus_connection` package is a pure interface — the
`ModbusConnection` / `ModbusUnit` [Protocols](https://typing.readthedocs.io/en/latest/spec/protocol.html),
the shared `WordOrder` type, and a tiny exception hierarchy. It imports **no**
Modbus library, so consumers can type against it without committing to a
backend.

Two interchangeable backends implement that interface:

| Backend | Module | Extra |
| --- | --- | --- |
| [pymodbus](https://github.com/pymodbus-dev/pymodbus) | `modbus_connection.pymodbus` | `[pymodbus]` |
| [tmodbus](https://github.com/wlcrs/tmodbus) | `modbus_connection.tmodbus` | `[tmodbus]` |

The bare install pulls neither backend.

## Why

One physical Modbus link addresses many units (1–247). Sharing a single,
internally-serialized connection across many consumers is strictly better than
each opening a competing socket. This package is the connection abstraction that
makes that sharing possible while keeping the backend swappable: the
`Protocol` never changes when the backend does.

## Design

- A connection is **transient** and **owner-held**. A backend *connect function*
  returns a live, already-connected instance — there is no `connect()` on the
  object.
- Requests are serialized per connection — but by the backend library, not by
  this wrapper: pymodbus's transaction manager and tmodbus's smart transport
  each hold a lock for the full request/response cycle, so concurrent unit calls
  on one connection can't interleave.
- A connection can enforce a minimum **gap between messages** for devices that
  need a pause between frames. Pass `message_spacing` (seconds) to a connect
  function and each request — from any unit sharing the link — waits until that
  gap has elapsed since the previous one finished. tmodbus enforces it through
  its native `wait_between_requests`; pymodbus has no such knob, so the package
  applies the same gap itself. It is the *spacing between* requests only; to
  delay the *first* request, the owner sleeps before issuing it. Default `0`
  disables it.
- The connection does **not** self-reconnect. On a drop it fires
  `on_connection_lost` (best-effort) and stops; recreating it is the owner's job.
- Consumers receive a **`ModbusUnit`** (via `connection.for_unit(unit_id)`), a
  stateless per-unit handle with no lifecycle methods. Every method **raises** on
  failure — it never returns `None`.
- The full 19-function-code Modbus surface is exposed — the register/coil reads
  and writes plus the diagnostic and identification codes (exception status,
  diagnostics, comm-event counter/log, report-server-id, FIFO queue, file
  records, device identification). A backend that cannot implement a code raises
  `NotImplementedError` (tmodbus does, for diagnostics and the comm-event codes).
  Datatype and word/byte-order decoding lives one layer up, in
  `modbus_connection.decode` / `.encode` and the `modbus_connection.model` device
  framework.

## Install

```bash
pip install "modbus-connection[pymodbus]"   # pymodbus backend
pip install "modbus-connection[tmodbus]"    # tmodbus backend
```

## Use

```python
import asyncio
from modbus_connection.pymodbus import connect_tcp


async def main() -> None:
    conn = await connect_tcp("192.168.1.50", port=502)
    try:
        unit = conn.for_unit(1)
        from modbus_connection.decode import decode_int16, decode_float32

        outside_temp = decode_int16(await unit.read_holding_registers(9, 1))
        flow_setpoint = decode_float32(await unit.read_holding_registers(40, 2))
        pump_on = (await unit.read_coils(56, 1))[0]
        print(outside_temp, flow_setpoint, pump_on)
    finally:
        await conn.close()


asyncio.run(main())
```

Swapping to tmodbus is a one-line import change:

```python
from modbus_connection.tmodbus import connect_tcp
```

## Transports

Each backend ships a set of connect functions, one per wire transport:

| Function | Transport | `framer` options |
| --- | --- | --- |
| `connect_tcp(host, *, port=502, framer="socket")` | Modbus TCP, or RTU-/ASCII-over-TCP (transparent serial-to-Ethernet gateways) | `socket` / `rtu` / `ascii` |
| `connect_udp(host, *, port=502, framer="socket")` | Modbus UDP (MBAP, RTU, or ASCII framing over UDP) | `socket` / `rtu` / `ascii` |
| `connect_serial(port, *, framer="rtu", baudrate=…, bytesize=…, parity=…, stopbits=…)` | Modbus serial — binary RTU or ASCII transmission mode | `rtu` / `ascii` |
| `connect_tls(host, *, port=802, sslctx=None, certfile=None, keyfile=None, password=None)` | Modbus/TLS (Modbus Security) | — (always TLS framing) |

`framer` names the wire framing across every transport (its value set differs by
transport: `socket`/`rtu`/`ascii` for TCP/UDP, `rtu`/`ascii` for serial; TLS is
fixed).

```python
from modbus_connection.pymodbus import connect_udp, connect_serial, connect_tls

udp = await connect_udp("192.168.1.50", port=502)
ascii_serial = await connect_serial("/dev/ttyUSB0", framer="ascii", baudrate=9600)
tls = await connect_tls("192.168.1.50", certfile="client.crt", keyfile="client.key")
```

For `connect_tls`, pass a fully-configured `ssl.SSLContext` as `sslctx` to control
server verification and trust; otherwise one is built from the optional client
`certfile` / `keyfile` / `password` (the default context does not verify the
server certificate).

tmodbus exposes the same functions, except `connect_udp`, `connect_tls`, and
`connect_tcp(framer="ascii")` — tmodbus has no UDP, TLS, or ASCII-over-TCP
transport, so those raise `NotImplementedError`; use the pymodbus backend for
them.

## Exceptions

Both backends raise the same neutral types:

- `ModbusError` — base class.
- `ModbusConnectionError` — link down / not connected / transport failure.
- `ModbusTimeoutError` — request sent, no valid response in time.
- `ModbusExceptionError` — device returned a Modbus exception response
  (`.exception_code` carries the raw code).

## Device modelling (`modbus_connection.model`)

An optional, backend-neutral framework for mapping a device's registers and coils
to typed Python attributes and reading the whole device — or one sub-system — in
as few Modbus calls as possible. It talks only to a `ModbusUnit`, so it runs over
any backend (or the mock).

```python
from modbus_connection.model import Component, gauge, uint32, coil

class Meter(Component):
    voltage = gauge(0, 0.1, unit="V")        # scaled 16-bit
    current = gauge(1, 0.1, unit="A")
    energy = uint32(2, unit="Wh")            # 32-bit over two registers
    relay = coil(0, writable=True)

meter = Meter(unit)
await meter.async_update()                   # one block read
meter.voltage                                # float | None
await meter.write("relay", True)
```

Generic field types ship here — `integer`, `gauge`, `raw_register`, `uint32` /
`int32` / `uint64` / `int64`, `float32` / `float64`, `string`,
`enum` / `flags` (map to an `IntEnum` / `IntFlag`), and the bit fields `coil`
(FC01, writable) / `discrete_input` (FC02, read-only) — plus an optional `nan`
sentinel, `word_order` and `byte_order`.

Numeric fields decode affinely as `raw * scale + offset`. Pass `offset` for a
device that reports a shifted value (e.g. `gauge(0, 0.1, offset=-100)` for a
temperature stored as `raw * 0.1 - 100`); writable fields invert it as
`(value - offset) / scale`. Anything more exotic is a `RegisterField` subclass.

`writable=True` lets `write()` send a field. Pass a validator callable instead to
both mark the field writable and vet the value before each write — it is called
with the requested value and returns the value to actually write (vetted or
coerced), or raises to reject it, before anything reaches the device:

```python
def in_range(value: int) -> int:
    if not 0 <= value <= 100:
        raise ValueError(f"{value} out of range")
    return value

class Boiler(Component):
    setpoint = integer(0, writable=in_range)
```

We don't ship validators of our own; for ready-made ones, reach for
[probatio](https://github.com/frenck/probatio).

The SunSpec module `modbus_connection.model.sunspec` adds the same types pre-wired
with their "unimplemented" sentinels, plus the address types (`ipaddr` /
`ipv6addr` / `eui48`).

`word_order` selects the order of the 16-bit registers in a multi-register value
and `byte_order` the order of the two bytes within each register; both default to
`"big"` (the Modbus convention). Together they spell out all four byte
arrangements real devices use — ABCD, CDAB, BADC and DCBA for a two-register
value — so a device that byte-swaps within a register decodes correctly with
`byte_order="little"`.

Shaping that neither covers — composing or transforming a value, packed
dates/times — is left to the consumer via a private field + a `@property`, so
static typing stays exact. For example, prefixing a version register with a
hard-coded model name:

```python
from modbus_connection.model import Component, string

class Controller(Component):
    _firmware = string(10, 4)  # 4 registers of ASCII, e.g. "1.23"

    @property
    def model(self) -> str | None:
        firmware = self._firmware
        return f"TROVIS 5576 ({firmware})" if firmware is not None else None
```

### Writing registers

`Component.write(field, value)` writes a writable register or coil by attribute
name. For registers it picks the function code by payload width — FC06
(write-single-register) for a one-word value, FC16 (write-multiple-registers)
otherwise. Some devices honour only FC16, even for a single register; pass
`force_fc16=True` on the field to always use FC16:

```python
from modbus_connection.model import Component, integer

class Inverter(Component):
    # A device that honours only FC16, even for a single register.
    limit = integer(0, writable=True, force_fc16=True)
```

Override `write()` in a subclass for any device-specific write sequencing.

Each component can refresh independently and has its own update listeners. To
refresh several components that share a unit in one consolidated set of reads,
group them in a `ComponentGroup` and call
`async_update()` on it:

```python
group = ComponentGroup(unit, [water_heater, circuit_1, circuit_2, circuit_3])
await group.async_update()  # one pooled set of reads; each component notified
```

The `ComponentGroup` builds its pooled read plan from the components' static
layout on the first update and reuses it on every later poll. The component list,
their fields, and the ranges are read once and cached — mutating them after the
first update is not supported; build a new `ComponentGroup` (or `Component`)
instead.

### Readable address ranges

Reads are pooled into block reads — addresses close together are fetched in one
call. By default the planner merges anything within a small gap, which assumes
every address in between is readable. Many devices only answer reads inside
specific ranges, and a read that crosses a gap is rejected.

Declare the device's readable ranges and the planner merges **only within a
range**, never across a boundary, and still clips each read to the addresses
actually used. Set them as a class attribute (shared by every instance) or per
instance. A `ComponentGroup` reads the ranges off its components, so every
component in a group must declare the same ranges (it raises otherwise):

```python
class Thermostat(Component):
    # (low, high) inclusive. The device answers 0–6 and 9–40 but nothing in
    # between, so 7–8 are never read and a 0..40 block is split at the gap.
    register_ranges = ((0, 6), (9, 40))
    coil_ranges = ((0, 15),)

    model = integer(0)
    outside = gauge(9, 0.1, unit="°C")

group = ComponentGroup(unit, [thermostat])  # ranges come from the components
await group.async_update()
```

Leave them as the default `None` for devices with a contiguous map (plain
gap-based planning).

Two planning limits are tunable as `Component` class attributes (and validated
to agree across a `ComponentGroup`):

- **`max_gap`** (default `16`) — only used in gap-based planning (no ranges):
  fields within this many addresses share one read. Higher means fewer requests
  but more over-reading; lower is safer for devices that reject reads of unmapped
  registers. (With `register_ranges` declared, `max_gap` is ignored.)
- **`max_span`** (default `125`, the Modbus per-request ceiling) — the widest a
  single block read may be. Lower it for a gateway that caps reads shorter.

### Repeated sub-units (`stride` / `index`)

Devices that expose several identical sub-units — heating circuits, channels,
phases — repeat the same registers at a fixed step. Model the sub-unit once and
instantiate it per index: pass `index` (1-based) to `Component(...)`, and give
each field a `stride` (the address step between sub-units for *that* register).
The absolute address read is `field.address + field.stride * (index - 1)`.

Each field carries its own `stride` because devices usually group registers by
type, not by sub-unit — so one logical sub-unit's fields are interleaved across
the map at different steps:

```python
class Circuit(Component):
    flow_temp = gauge(12, 0.1, stride=1)          # circuits 1–3 at 12, 13, 14
    control_signal = integer(106, stride=2)       # ...        at 106, 108, 110
    flow_setpoint = gauge(999, 0.1, stride=200)   # ...        at 999, 1199, 1399

circuits = [Circuit(unit, index=n) for n in (1, 2, 3)]
```

A field with the default `stride=0` is at a fixed address shared by every index.

When instead *every* field of a sub-unit shares one step — the common case for a
self-contained, contiguous repeating block (e.g. a SunSpec multiple-MPPT module)
— pass `base_offset` rather than repeating the same `stride` on each field. It
shifts every field and bit address by a fixed amount, so you model the block once
at instance 0's addresses and read instance *i* with `base_offset = i * block_len`:

```python
class MPPTModule(Component):
    dc_w = integer(11, scale_register=2)   # one module; addresses are instance 0's
    dc_v = integer(10, scale_register=1)

modules = [MPPTModule(unit, base_offset=i * 20) for i in range(n)]
```

`base_offset` composes additively with `index` / `stride` and applies to reads
and writes alike. Scale-factor registers (`scale_register`) are **not** shifted —
a SunSpec repeating block's scale factors live in the shared fixed block, so they
keep their absolute address (a per-instance scale register stays governed by
`scale_register_stride`).

### Runtime-counted repeats (`repeating_group`)

`stride` / `base_offset` cover repeats whose **count is known when you write the
code**. Some devices instead advertise the count in a register, read at poll time
— a SunSpec multiple-MPPT model (160) carries an `N` point saying how many modules
follow. `repeating_group` is a field for that: model one instance as a `Component`,
and the parent reads the count each poll and exposes a `list` of that many
instances, each fully typed:

```python
from modbus_connection.model import Component, integer, repeating_group
from modbus_connection.model.sunspec import uint16

class MPPTModule(Component):                 # one module, at instance 0's addresses
    dc_w = integer(11, scale_register=2)
    dc_v = integer(10, scale_register=1)

class Inverter(Component):
    modules = repeating_group(uint16(8), MPPTModule, stride=20)  # N at register 8

inv = Inverter(unit)
await inv.async_update()
inv.modules                # list[MPPTModule]
inv.modules[0].dc_w        # typed per-instance access
await inv.modules[2].write("dc_w", ...)   # writes go through the instance
```

`count` is a `RegisterField` (read each poll) or a fixed `int`; instance *i* is
read at `base_offset = i * stride`, so `stride` is the block length. A fixed
`int` count is static, so its instances fold into the component's normal read.
A `RegisterField` count needs a second pass — the count is read first, then the
sized-out instances (pooled among themselves) — since the count must be known
before the instances it sizes can be planned. An unimplemented or unreadable
count yields no instances. A component with a `repeating_group` refreshes on its
own; it is not pooled into a `ComponentGroup`.

### Register spaces (holding vs input)

A component's register fields default to the **holding** space (FC03). For a
read-only sub-system whose data lives in **input** registers (FC04), set
`register_space = "input"` on the component — fields and factories are
unchanged:

```python
class Sensors(Component):
    register_space = "input"
    flow_temp = gauge(5, 0.1, unit="°C")   # read with FC04
```

Input and holding are separate address spaces (input 507 ≠ holding 507), so the
planner never merges them into one read, and `register_ranges` applies within the
component's own space. A `ComponentGroup` may mix input and holding components: it
reads each space with its own block reads, and components only need matching
`register_ranges` with others in the *same* space. Input registers are physically
read-only, so writing a field on an `"input"` component raises.

### Bit spaces (coils vs discrete inputs)

Bits work the same way over their own pair of spaces: `coil` fields are read and
written via the **coils** space (FC01), and `discrete_input` fields are read from
the **discrete inputs** space (FC02, read-only). The space is carried by the field
type, so a single component may declare both — they are planned and read
separately (coil 12 ≠ discrete input 12), exactly like input vs holding registers:

```python
class IO(Component):
    relay = coil(0, writable=True)       # FC01, read/write
    fault = discrete_input(0)            # FC02, read-only — distinct from coil 0
```

Discrete inputs are physically read-only, so writing a `discrete_input` field
raises. The two bit spaces have their own readable maps, so `coil_ranges`
constrains coils and `discrete_ranges` constrains discrete inputs.

### Runtime-built groups (`ManualComponent`)

When the field layout comes from config (e.g. YAML) rather than a typed class —
there's no `Component` subclass to declare — use a `ManualComponent`. It's the
imperative twin: add targets by key at runtime and it pools them into as few
reads as possible, mixing all four tables (holding, input, coils, discrete
inputs) in one update.

```python
mc = ManualComponent(unit, max_gap=16)
mc.add("flow_temp", gauge(40, 0.1))                 # holding (default)
mc.add("energy",    uint32(2),  space="input")      # input registers
mc.add("relay",     coil(5, writable=True))         # coils (FC01)
mc.add("alarm",     discrete_input(9))              # discrete inputs (FC02)

data = await mc.async_update()    # {"flow_temp": 21.5, "energy": 100000, ...}
mc.get("flow_temp")               # 21.5
await mc.write("relay", True)     # per-key write (holding / coils only)
```

A register target takes its `space` (`"holding"` / `"input"`) on `add()`; a bit
target's space is fixed by the factory (`coil` / `discrete_input`). The field
`address` is absolute (no `index` / `stride`), values come out via `get(key)` and
the dict `async_update()` returns (no typed attribute access — there's no class),
and `add()` / `remove()` invalidate the cached plan so it re-plans on the next
update. It reuses the same planning, write (validator / `force_fc16`) and bit
machinery as `Component`; it does not pool into a `ComponentGroup`. Readable
ranges are per-table kwargs — `holding_ranges` / `input_ranges` / `coil_ranges`
/ `discrete_ranges` (any left unset falls back to gap-based planning):

```python
ManualComponent(unit, holding_ranges=((0, 40),), input_ranges=((500, 520),))
```

## Testing

An in-memory mock backend ships as a `pytest` plugin (auto-registered via an
entry point — no `conftest` wiring). It implements the same Protocols, so code
typed against `ModbusUnit` runs against it unchanged.

```python
async def test_reads_setpoint(mock_modbus_unit):
    mock_modbus_unit.holding[40] = 1234            # single value
    mock_modbus_unit.holding[2] = [0x0001, 0x86A0]  # list -> consecutive registers
    mock_modbus_unit.holding[9] = lambda: 7         # callable -> evaluated per read

    assert await mock_modbus_unit.read_holding_registers(40, 1) == [1234]
    assert await mock_modbus_unit.read_holding_registers(2, 2) == [0x0001, 0x86A0]
```

Reads resolve against the per-space stores (`holding`, `input`, `coils`,
`discrete_inputs`); writes mutate them and fire `on_write` callbacks, so a test
can react to a write by mocking other registers:

```python
def test_command_sets_ready(mock_modbus_unit):
    def respond(event):
        if event.address == 0:          # a command was written
            mock_modbus_unit.holding[100] = 1   # device flips its "ready" flag
    mock_modbus_unit.on_write(respond)
```

To simulate a device **rejecting a write**, arm `fail_write`. The next write
covering that address raises the given error *before* the store is touched, so
the value is left unchanged and `on_write` callbacks don't fire. `register_type`
defaults to `"holding"` (use `"coil"` for coil writes — the two tables are
independent); pass `None` to clear.

```python
async def test_write_rejected(mock_modbus_unit):
    mock_modbus_unit.holding[40] = 7
    mock_modbus_unit.fail_write(40, ModbusExceptionError(3))  # illegal data value
    with pytest.raises(ModbusExceptionError):
        await mock_modbus_unit.write_register(40, 99)
    assert await mock_modbus_unit.read_holding_registers(40, 1) == [7]  # unchanged

    mock_modbus_unit.fail_write(40, None)                     # clear it
    await mock_modbus_unit.write_register(40, 99)             # now succeeds
```

To simulate a **read failing**, give the register a callable that raises — it's
evaluated on every read:

```python
def boom():
    raise ModbusExceptionError(2)       # illegal data address
mock_modbus_unit.holding[9] = boom
```

Fixtures: `mock_modbus_connection` (a `MockModbusConnection`) and
`mock_modbus_unit` (its unit 1). `MockModbusConnection` / `MockModbusUnit` are
also importable from `modbus_connection.mock` for direct construction.

## Develop

```bash
uv sync --extra pymodbus
uv run pytest
```

Formatting/linting is [ruff](https://docs.astral.sh/ruff/), enforced in CI. Install
the commit hook with [prek](https://github.com/j178/prek) so code is formatted on
commit:

```bash
uvx prek install          # set up the git hook
uvx prek run --all-files  # format + lint everything now
```
