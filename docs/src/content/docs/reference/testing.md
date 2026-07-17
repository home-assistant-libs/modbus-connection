---
title: Testing
description: The in-memory mock backend — a pytest plugin that implements the same Protocols, so device code runs against it unchanged.
---

An in-memory **mock backend** ships as a `pytest` plugin. It's auto-registered via
an entry point — no `conftest` wiring — and it implements the same
`ModbusConnection` / `ModbusUnit` Protocols, so code typed against `ModbusUnit`
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
    mock_modbus_unit.holding[40] = 1234             # single value
    mock_modbus_unit.holding[2] = [0x0001, 0x86A0]  # list -> consecutive registers
    mock_modbus_unit.holding[9] = lambda: 7         # callable -> evaluated per read

    assert await mock_modbus_unit.read_holding_registers(40, 1) == [1234]
    assert await mock_modbus_unit.read_holding_registers(2, 2) == [0x0001, 0x86A0]
```

Reads resolve against these stores; writes mutate them and fire `on_write`
callbacks.

## Replaying a raw snapshot

A raw dump from
[`async_read_raw()`](/modbus-connection/modelling/component-group/#raw-diagnostics)
— e.g. captured from a real device in a bug report — loads straight into the mock
with `load_raw()`, so you can reproduce that device and check your model decodes
it. It maps the `{space: {address: value}}` format onto the four stores (`"coil"`
/ `"discrete"` go to `coils` / `discrete_inputs`):

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
    mock_modbus_unit.holding[0] = 2301      # raw
    meter = Meter(mock_modbus_unit)
    await meter.async_update()
    assert meter.voltage == 230.1           # raw * 0.1
```

## Reacting to writes

Register an `on_write` callback to simulate a device that changes state in
response to a command — e.g. flips a "ready" flag when a command register is
written:

```python
def test_command_sets_ready(mock_modbus_unit):
    def respond(event):
        if event.address == 0:                     # a command was written
            mock_modbus_unit.holding[100] = 1      # device flips its "ready" flag
    mock_modbus_unit.on_write(respond)
```

## Simulating a rejected write

Arm `fail_write` and the next write covering that address raises the given error
*before* the store is touched, so the value is left unchanged and `on_write`
callbacks don't fire. `register_type` defaults to `"holding"` (use `"coil"` for
coil writes — the tables are independent); pass `None` to clear:

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

## Simulating a read failure

Arm `fail_read` and any read whose block covers that address raises the given
error instead of returning values — mirroring a device that refuses a register
block it doesn't serve, such as an uninstalled module. `register_type` defaults
to `"holding"` (use `"input"`, `"coil"` or `"discrete_input"` for the other
tables — they're independent); pass `None` to clear:

```python
async def test_read_refused(mock_modbus_unit):
    mock_modbus_unit.fail_read(1100, ModbusExceptionError(2))  # illegal data address
    with pytest.raises(ModbusExceptionError):
        await mock_modbus_unit.read_holding_registers(1100, 4)
    await mock_modbus_unit.read_holding_registers(0, 4)        # other blocks unaffected

    mock_modbus_unit.fail_read(1100, None)                     # clear it
```

## Why it matters

The mock lets a device library's tests cover the hard part — the register map, the
scaling, the write sequencing, the pooled read plan — with plain `pytest` and no
device. Keep that library separate from any Home Assistant integration and this is
where nearly all your coverage lives; see
[Integration structure](/modbus-connection/home-assistant/integration/).
