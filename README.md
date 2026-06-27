# modbus-connection

A small, **backend-neutral** Modbus connection abstraction.

The top-level `modbus_connection` package is a pure interface — the
`ModbusConnection` / `ModbusUnit` [Protocols](https://typing.readthedocs.io/en/latest/spec/protocol.html),
the shared `WordOrder` type, and a tiny exception hierarchy. It imports **no**
Modbus library and **no** Home Assistant, so consumers can type against it
without committing to a backend.

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
- A connection can enforce a minimum **interval between messages** for devices
  that need a pause between frames. Pass `message_spacing` (seconds) to a connect
  function and consecutive requests — from any unit sharing the link — are kept at
  least that far apart. It is the *spacing between* requests only; to delay the
  *first* request, the owner sleeps before issuing it. Default `0` disables it.
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
`enum` / `flags` (map to an `IntEnum` / `IntFlag`), and `coil` (plus an optional
`nan` sentinel and `word_order`). The SunSpec
module `modbus_connection.model.sunspec` adds the same types pre-wired with their
"unimplemented" sentinels, plus the address types (`ipaddr` / `ipv6addr` /
`eui48`).

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

Each component can refresh independently and has its own update listeners (one
Home Assistant entity per component). To refresh several components that share a
unit in one consolidated set of reads, group them in a `ComponentGroup` and call
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
