---
title: Testing
description: The in-memory mock backend — a pytest plugin that implements the same connection and unit APIs.
---

An in-memory **mock backend** ships as a `pytest` plugin. It's auto-registered via
an entry point — no `conftest` wiring — and it implements the same
`ModbusConnection` / `ModbusUnit` APIs, so code typed against `ModbusUnit`
runs against it unchanged. This is how you test a device library with no hardware
and no Home Assistant in the loop.

## Fixtures

- `mock_modbus_connection` — a `MockModbusConnection`.
- `mock_modbus_unit` — its unit 1.

`MockModbusConnection` / `MockModbusUnit` are also importable from
`modbus_connection.mock` for direct construction.

## Seeding registers

Set values on the per-space stores (`holding`, `input`, `coils`,
`discrete_inputs`). A single value fills one register; a list fills consecutive
registers; a callable is evaluated on every read:

```python
async def test_reads_setpoint(mock_modbus_unit):
    mock_modbus_unit.holding[40] = 1234  # single value
    mock_modbus_unit.holding[2] = [0x0001, 0x86A0]  # list -> consecutive registers
    mock_modbus_unit.holding[9] = lambda: 7  # callable -> evaluated per read

    assert await mock_modbus_unit.read_holding_registers(40, 1) == [1234]
    assert await mock_modbus_unit.read_holding_registers(2, 2) == [0x0001, 0x86A0]
```

Reads resolve against these stores; writes mutate them and fire `on_write`
callbacks.

## Replaying a raw snapshot

A raw dump from
[`async_read_raw()`](/modbus-connection/modelling/overview/#raw-diagnostics)
— e.g. captured from a real device in a bug report — loads straight into the mock
with `load_raw()`, so you can reproduce that device and check your model decodes
it. The dump is keyed by the four Modbus spaces — `holding`, `input`, `coil`,
`discrete` — which `load_raw()` maps onto the stores:

```python
async def test_decodes_a_captured_device(mock_modbus_unit):
    mock_modbus_unit.load_raw({"holding": {0: 2301}, "coil": {0: True}})
    meter = Meter(mock_modbus_unit)
    await meter.async_update()
    assert meter.voltage == 230.1
```

## Testing a component

Because the mock is a real `ModbusUnit`, you test a `Component` exactly as
production code uses it:

```python
from modbus_connection.model import Component, gauge


class Meter(Component):
    voltage = gauge(0, 0.1, unit="V")


async def test_meter(mock_modbus_unit):
    mock_modbus_unit.holding[0] = 2301  # raw
    meter = Meter(mock_modbus_unit)
    await meter.async_update()
    assert meter.voltage == 230.1  # raw * 0.1
```

## Reacting to writes

Register an `on_write` callback to simulate a device that changes state in
response to a command — e.g. flips a "ready" flag when a command register is
written:

```python
def test_command_sets_ready(mock_modbus_unit):
    def respond(event):
        if event.address == 0:  # a command was written
            mock_modbus_unit.holding[100] = 1  # device flips its "ready" flag

    mock_modbus_unit.on_write(respond)
```

## Asserting on the reads a poll issued

`read_events` logs every block read the unit received, in order, as a
[`ReadEvent`](#readevent). Where `async_read_raw()` reports which *addresses* a
poll covered, this reports the **blocks the planner actually asked for** — so a
test can pin down how many round-trips a poll costs, and how wide each one was:

```python
async def test_poll_respects_the_controller_limits(mock_modbus_unit):
    await MyDevice(mock_modbus_unit).async_update()

    blocks = mock_modbus_unit.read_events
    assert len(blocks) == 3  # the whole map in three round-trips
    assert all(b.count <= 100 for b in blocks)  # controller caps a read at 100
    assert all(b.register_type == "holding" for b in blocks)  # no coils on this device
```

That is the assertion a device library wants when its controller only answers
[declared ranges](/modbus-connection/modelling/overview/#readable-address-ranges)
or caps a request's width — the log is the read-side counterpart of `on_write`,
and needs no wrapper around the unit.

A read is recorded when it is dispatched, so one the device then rejects (see
[`fail_read`](#simulating-a-read-failure)) still appears: the request went out.

## Simulating a rejected write

Arm `fail_write` and the next write covering that address raises the given error
*before* the store is touched, so the value is left unchanged and `on_write`
callbacks don't fire. `register_type` defaults to `"holding"` (use `"coil"` for
coil writes — the tables are independent); pass `None` to clear:

Arm the [typed exception](/modbus-connection/connection/reference/#modbusexceptionerror)
for the condition — it constructs with its code implied, and it is what the
backends raise:

```python
async def test_write_rejected(mock_modbus_unit):
    mock_modbus_unit.holding[40] = 7
    mock_modbus_unit.fail_write(40, IllegalDataValueError())
    with pytest.raises(IllegalDataValueError):
        await mock_modbus_unit.write_register(40, 99)
    assert await mock_modbus_unit.read_holding_registers(40, 1) == [7]  # unchanged

    mock_modbus_unit.fail_write(40, None)  # clear it
    await mock_modbus_unit.write_register(40, 99)  # now succeeds
```

The error you arm is the condition you're simulating:

```python
mock_modbus_unit.fail_write(40, IllegalDataValueError())  # device rejects the value
mock_modbus_unit.fail_write(40, ModbusTimeoutError())  # device doesn't answer
mock_modbus_unit.fail_write(40, ModbusConnectionError())  # device unreachable
mock_modbus_unit.fail_write(40, ModbusProtocolError())  # corrupt reply
```

## Simulating a read failure

Arm `fail_read` and any read whose block covers that address raises the given
error instead of returning values — mirroring a device that refuses a register
block it doesn't serve, such as an uninstalled module. `register_type` defaults
to `"holding"` (use `"input"`, `"coil"` or `"discrete_input"` for the other
tables — they're independent); pass `None` to clear:

```python
async def test_read_refused(mock_modbus_unit):
    mock_modbus_unit.fail_read(1100, IllegalDataAddressError())
    with pytest.raises(IllegalDataAddressError):
        await mock_modbus_unit.read_holding_registers(1100, 4)
    await mock_modbus_unit.read_holding_registers(0, 4)  # other blocks unaffected

    mock_modbus_unit.fail_read(1100, None)  # clear it
```

## Simulating a dropped link

`simulate_connection_lost()` on the connection drops the link and fires every
`on_connection_lost` callback — for testing code that observes the transport,
like a coordinator marking entities unavailable. The drop is transient, as it is
on a real connection: the next request establishes the link again.

```python
async def test_reacts_to_a_drop(mock_modbus_connection, mock_modbus_unit):
    events = []
    mock_modbus_connection.on_connection_lost(lambda: events.append("lost"))

    mock_modbus_connection.simulate_connection_lost()
    assert events == ["lost"]
    assert mock_modbus_connection.connected is False

    await mock_modbus_unit.read_holding_registers(0, 1)  # reconnects on demand
    assert mock_modbus_connection.connected is True
```

`close()` behaves like the real thing too: it is permanent, does not fire the
callbacks, and later requests raise `ClientClosedError`. `disconnect()` also
matches the real connection — it drops the link without firing the callbacks,
and the next request reconnects.

## Canned responses for the other operations

The register and bit operations resolve against the stores, but the
diagnostic, file-record, and identification operations have no natural store.
Arm each one you use with `set_response(method, value)` — a callable value is
evaluated per call — or the mock raises `NotImplementedError` telling you which
response to configure:

```python
async def test_reads_server_id(mock_modbus_unit):
    mock_modbus_unit.set_response("report_server_id", b"\x11ACME v2")
    assert await mock_modbus_unit.report_server_id() == b"\x11ACME v2"
```

The operations that take a canned response: `read_exception_status`,
`report_server_id`, `read_fifo_queue`, `read_device_identification`,
`read_file_record`, `diagnostics`, `get_comm_event_counter`, and
`get_comm_event_log`. (`mask_write_register` and `read_write_registers` work
against the register stores directly, and `write_file_record` is accepted as a
no-op.)

## Mock API reference

### `MockModbusConnection`

Implements the full `ModbusConnection` API in memory — `connected`,
`for_unit(unit_id)`, `connect()`, `close()`, and
`on_connection_lost(callback)` — plus the test hook
[`simulate_connection_lost()`](#simulating-a-dropped-link). `for_unit` returns
**the same `MockModbusUnit` per unit id**, so the unit you seed is the unit the
code under test reads.

### `MockModbusUnit`

Implements the full `ModbusUnit` API against in-memory stores, plus the test
configuration surface:

| Member | What it does |
| --- | --- |
| `holding`, `input`, `coils`, `discrete_inputs` | The per-space stores: `dict` of address to a value, a list (consecutive addresses), or a callable (evaluated per read) — [`RegisterSpec`](#registerspec-and-coilspec) / [`CoilSpec`](#registerspec-and-coilspec). |
| `on_write(callback)` | Register a callback invoked with a [`WriteEvent`](#writeevent) for register and coil writes; returns an unsubscribe callable. |
| `read_events` | The [`ReadEvent`](#readevent) log of [every block read](#asserting-on-the-reads-a-poll-issued) the unit received, in order. |
| `fail_write(address, error, *, register_type="holding")` | Arm the exception matching writes raise (`"holding"` or `"coil"`); `None` clears it. |
| `fail_read(address, error, *, register_type="holding")` | Arm the exception reads covering the address raise (`"holding"`, `"input"`, `"coil"`, or `"discrete_input"`); `None` clears it. |
| `set_response(method, value)` | Arm a [canned response](#canned-responses-for-the-other-operations) for a non-store operation. |
| `load_raw(raw)` | Load an [`async_read_raw()` snapshot](#replaying-a-raw-snapshot) into the stores; raises `ValueError` for an unknown space. |
| `set_message_spacing(seconds)` | Records the interval on the `message_spacing` attribute for assertions; raises `ValueError` if negative. |

### `WriteEvent`

The frozen dataclass `on_write` callbacks receive:

| Field | Type | Meaning |
| --- | --- | --- |
| `register_type` | `"holding" \| "coil"` | Which table was written. |
| `address` | `int` | The first written address. |
| `values` | `list[int] \| list[bool]` | The written values, one per address. |
| `function_code` | `int` | The function code the write went out as: `0x06`/`0x10` for registers (`force_fc16` makes a one-register write `0x10`), `0x05`/`0x0F` for coils, `0x16` for a mask write. |

### `ReadEvent`

The frozen dataclass `read_events` collects:

| Field | Type | Meaning |
| --- | --- | --- |
| `register_type` | `"holding" \| "input" \| "coil" \| "discrete_input"` | Which table was read. |
| `address` | `int` | The block's first address. |
| `count` | `int` | How many addresses the block covers. |

### `RegisterSpec` and `CoilSpec`

The store value types: `int | list[int] | Callable[[], int | list[int]]` for
the register stores, and the `bool` equivalent for the bit stores.

## Why it matters

The mock lets a device library's tests cover the hard part — the register map, the
scaling, the write sequencing, the pooled read plan — with plain `pytest` and no
device. Keep that library separate from any Home Assistant integration and this is
where nearly all your coverage lives; see
[Integration structure](/modbus-connection/home-assistant/integration/).
