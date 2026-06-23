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
- The connection owns the single `asyncio.Lock` that serializes every request.
- The connection does **not** self-reconnect. On a drop it fires
  `on_connection_lost` (best-effort) and stops; recreating it is the owner's job.
- Consumers receive a **`ModbusUnit`** (via `connection.for_unit(unit_id)`), a
  stateless per-unit handle with no lifecycle methods. Every method **raises** on
  failure — it never returns `None`.
- The full 19-function-code Modbus surface is exposed, plus typed reads
  (`read_uint16`, `read_float32`, …) that own datatype + word/byte ordering. A
  backend that cannot implement a code raises `NotImplementedError`.

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
        outside_temp = await unit.read_int16(9)        # raw register, signed
        flow_setpoint = await unit.read_float32(40, word_order="big")
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

## Testing

An in-memory mock backend ships as a `pytest` plugin (auto-registered via an
entry point — no `conftest` wiring). It implements the same Protocols, so code
typed against `ModbusUnit` runs against it unchanged.

```python
async def test_reads_setpoint(mock_modbus_unit):
    mock_modbus_unit.holding[40] = 1234            # single value
    mock_modbus_unit.holding[2] = [0x0001, 0x86A0]  # list -> consecutive registers
    mock_modbus_unit.holding[9] = lambda: 7         # callable -> evaluated per read

    assert await mock_modbus_unit.read_uint16(40) == 1234
    assert await mock_modbus_unit.read_uint32(2) == 100000
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
