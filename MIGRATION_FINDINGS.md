# Migrating Home Assistant's `modbus` integration onto `modbus-connection`

A proof-of-concept migration of the core `modbus` integration to use
`modbus-connection` (latest `main`, unreleased) as its internal connection layer.
Goal: exercise the library against a large, real consumer and surface what's
missing.

**Result:** the full read/write surface mapped cleanly and the entire integration
test suite passes (**451 passed, 7 skipped**) after adapting the tests to the new
call shape. The notes below are the friction and gaps found along the way.

## What was migrated

- `ModbusHub` (in `homeassistant/components/modbus/modbus.py`) no longer talks to
  `pymodbus` directly. It holds a `ModbusConnection` and dispatches each call
  through `connection.for_unit(unit).<method>()`.
- `async_pb_call()` now returns plain `list[int]` / `list[bool]` (reads) or `[]`
  (write success) / `None` (failure) instead of a `pymodbus` PDU. The platforms
  (`binary_sensor`, `sensor`, `cover`, `light`, `climate`, `entity`) were updated
  to consume the lists directly instead of `result.registers` / `result.bits`.
- Transport/framer selection maps to the connect functions:
  `TCP→connect_tcp(framer="socket")`, `rtuovertcp→connect_tcp(framer="rtu")`,
  `UDP→connect_udp`, `serial→connect_serial(framer="rtu"|"ascii")`.

## What worked well

- **1:1 function-code surface.** The integration's eight call types map directly
  onto `read_coils` / `read_discrete_inputs` / `read_holding_registers` /
  `read_input_registers` / `write_register(s)` / `write_coil(s)`. No impedance
  mismatch.
- **`message_spacing` (unreleased) replaced bespoke pacing.** The hub previously
  held an `asyncio.Lock` and `await asyncio.sleep(self._msg_wait)` after every
  request to space frames (and defaulted to 30 ms on serial). Passing
  `message_spacing=` to the connect function deletes all of that — the library
  paces across every unit on the link itself. This is the single biggest
  simplification and exactly the kind of shared-link concern the library is meant
  to own.
- **Neutral exception hierarchy.** The hub's "log and return `None`" behavior maps
  to a single `except ModbusError`. `ModbusExceptionError.exception_code` and the
  timeout/connection split are richer than what the integration currently uses.
- **`for_unit(unit_id)` per call is free.** A stateless handle per call replaced
  the per-call `device_id` kwarg juggling cleanly.
- **Reads return clipped `list`s.** No more reaching into PDU attributes;
  `read_coils` already does `bits[:count]`.

## Resolved since the first pass

- **connect/close now map to the neutral hierarchy (#21).** `connect_*()` routes
  construction + `connect()` through a shared `_open()` that maps a raising
  constructor / raising `connect()` / falsy `connect()` onto `ModbusConnectionError`
  (bad-config `ParameterException` → `ValueError`, connect timeout →
  `ModbusTimeoutError`), and `close()` maps teardown errors too. The hub went back
  from a bare `except Exception` in teardown to `except ModbusError`, and its
  connect/retry loop now catches `ModbusError` so a connect-time timeout retries
  instead of escaping. This was the only genuine request-vs-lifecycle inconsistency
  and it's gone.
- **`ModbusTimeoutError` is now a `TimeoutError`**, so callers can catch either the
  neutral type or the stdlib one.
- **Repeating sub-units have first-class support (#19/#20/#22):** `repeating_group`
  (a runtime-counted list of sub-components) plus `Component.base_offset` (a uniform
  per-instance address shift). This is the missing piece for the `slave_count` /
  "virtual" fan-out below.

## Withdrawn: "no self-reconnect" is by design, not a gap

The connection is deliberately transient and owner-held; the README says so and
`reconnect_delay=0` is intentional. The hub's `_handle_connection_lost` → recreate
loop is ~10 lines and stashing the connect params is trivial. This is our job and
it's fine — I retract framing it as a blocker.

## Remaining, minor

### `connect_*()` exposes no `retries`
pymodbus's client took `retries=3`; the connect functions don't surface it (they
inherit the pymodbus default, also 3, so behavior is preserved — just no longer
tunable). Some gateways want a different count. Low priority.

### Stricter write typing than pymodbus
`write_coil(value: bool)` / `write_coils(values: list[bool])` are typed as bools;
the integration historically passed ints (command-on values), so the migration
coerces with `bool(...)`. A contract to note when porting, not a defect.

### `_check`/reads assume a well-formed PDU
Reads do `response.bits[:count]` with no guard; a `None`-payload PDU yields a
`TypeError` rather than a mapped error. Real pymodbus never does this (it showed up
only in the integration's "unavailable" test fictions, which we changed to model
unavailability as an error response). Fine to leave to the backend.

## Correction: the `model` layer fits better than I first said

I originally listed the integration's custom `struct` decoding as something
`ManualComponent` couldn't express. **That was wrong.** `RegisterField[T]` is an ABC
whose codec is `decode(self, words: list[int]) -> Any` / `encode(...) -> list[int]`,
and `ManualComponent.add(key, target: RegisterField[Any] | _BitField, ...)` accepts
*any* subclass — so a custom `StructField` that runs the integration's
`struct`/swap/offset logic and returns whatever it likes drops straight in. Raw
access exists at three levels: the raw `ModbusUnit.read_*` → `list[int]`; the
`modbus_connection.decode`/`.encode` helpers over `list[int]`; and a custom
`RegisterField` subclass (or the shipped `raw_register`, a single undecoded word).

What the model layer *still* doesn't do for free is **architectural**, not decoding:

- **Per-entity presentation** (`min/max` clamp, `zero_suppress`, `precision`→string,
  CSV join) lives above the field — it's how the sensor renders a decoded value, not
  a codec concern, so it stays in the entity regardless of backend.
- **Per-entity independent polling / scan_interval / verify-on-write** vs one pooled
  per-unit `async_update()` — adopting the model means a per-`(hub, unit)`
  coordinator, i.e. a real (worthwhile) refactor, not a drop-in.

With `repeating_group` + `base_offset` now landed, the `slave_count` fan-out is
expressible too, so a `ManualComponent`-per-unit rewrite is now a credible follow-up
if pooled block reads are the goal.

**Note (#24):** `byte_order` was removed from the codec (revert of #9) — the survey
found the only in-register byte-swap consumer in core is modbus's own `swap:`
option, which core keeps implementing itself. That confirms the current migration's
approach (raw `ModbusUnit` + the integration's `_swap_registers`) rather than
pushing swap into the field layer.

## Summary

After #21 there are **no lifecycle blockers left** — connect/close map cleanly,
timeouts are typed, reconnect is (correctly) ours. The request surface never needed
anything. `message_spacing` remains the standout win. The open items are minor
(`retries` knob, bool-typing note). The one substantial future step is optional:
move from raw `ModbusUnit` to a `ManualComponent`-per-unit coordinator to pool reads
— now unblocked by the custom-decode escape hatch and `repeating_group`.
