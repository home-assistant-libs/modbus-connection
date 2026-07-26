---
title: Integration structure
description: How modbus-connection fits a Home Assistant integration.
---

modbus-connection is a clean foundation for a **built-in Home Assistant
integration** — one that ships in Home Assistant Core. The split it enforces — a
connection owned at the top, stateless units handed down, typed components over
them — lines up exactly with how Home Assistant wants a device integration
structured.

:::caution[Shared connections are coming]
Home Assistant is building a system that lets integrations **share one Modbus
connection** rather than each opening its own. It is not finished, so it is not
what this page shows — the example below has your integration own its connection.
The structure is deliberately chosen so that migrating is straightforward: the
device library only ever sees a `ModbusUnit`, and the connection is built in one
place (`async_setup_entry`) from values your config flow collected. When sharing
lands, that one place changes; the library, coordinator and entities do not.
:::

:::note[Read the official guide first]
Home Assistant maintains a dedicated guide for Modbus-based integrations. Read it
alongside this page — it covers the coordinator pattern, entity setup, and config
flow in Home Assistant terms:

**[developers.home-assistant.io → Modbus integration](https://developers.home-assistant.io/docs/modbus/introduction)**
:::

## The library requirement

A built-in integration **may not talk to the device directly**. Home Assistant
Core requires all protocol and device communication to live in a **separate
library published to PyPI**; the integration itself is a thin layer that wires
that library to Home Assistant's entities, config flow and coordinator.

That requirement is precisely the [library entrypoint pattern](/modbus-connection/patterns/library/):
a standalone package, built on modbus-connection, that exposes a device object
over `Component`s and consumes a `ModbusUnit`. Build that library first — it is
what the integration will `import` and list in its `manifest.json` requirements.

## The recommended layering

An integration built this way has three clear layers:

1. **modbus-connection** — the connection + modelling foundation the library is
   built on.
2. **A device library** (its own PyPI package) — the
   [entrypoint pattern](/modbus-connection/patterns/library/): a top-level device
   object over `Component`s, backend-neutral, consuming a `ModbusUnit`. This has
   **no Home Assistant dependency** and is released and tested on its own.
3. **Your device integration** (in `homeassistant/components/<domain>/`) — owns
   the `ModbusConnection`, gathers its connection details in its own config flow,
   hands a `ModbusUnit` to the library, and polls it from a
   `DataUpdateCoordinator`.

Keeping the device library separate is not just good practice here — it is a
condition for merging into Core, and it means the hard part (the register map)
gets tested against the [mock](/modbus-connection/reference/testing/) with no Home
Assistant in the loop.

:::note[Custom integrations]
A custom integration is not bound by the separate-library rule — you can ship the
device code inside the integration itself. We still recommend modelling it as its
own library: it keeps the register map testable without Home Assistant, and it is
what you would need anyway to submit the integration to Core later.
:::

## Your integration owns the connection

There is no shared connection integration in Home Assistant: **each integration
opens and owns its own link**. Your config flow therefore collects everything
needed to build one, your `async_setup_entry` constructs it, and unloading the
entry closes it.

Because the integration imports a concrete backend
([tmodbus or pymodbus](/modbus-connection/getting-started/backends/)), make sure
that backend's extra is actually installed — either as your device library's own
dependency (`modbus-connection[tmodbus]`) or as a `manifest.json` requirement.

### The config flow

Collect the transport details for the params object your integration builds —
`CONF_HOST` / `CONF_PORT` for TCP, the serial device and baud rate for RTU — plus
the **unit id where the user can choose it**. A device with a fixed station
address, which is common for a TCP-native device, keeps that address as a
constant in the integration instead of asking for it.

Validate the input by actually talking to the device, and close the connection
you opened for the check:

```python
from modbus_connection import ModbusError, ModbusTcpParams
from modbus_connection.tmodbus import ModbusConnection


async def _async_probe(host: str, port: int, unit_id: int) -> str:
    """Return the device serial, or raise ModbusError if it can't be reached."""
    connection = ModbusConnection(ModbusTcpParams(host=host, port=port))
    try:
        return await MyDevice.async_probe(connection.for_unit(unit_id))
    finally:
        await connection.close()


class MyConfigFlow(ConfigFlow, domain=DOMAIN):
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                serial = await _async_probe(
                    user_input[CONF_HOST],
                    user_input[CONF_PORT],
                    user_input[CONF_UNIT_ID],
                )
            except ModbusError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(serial)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=serial, data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )
```

Constructing a connection performs no I/O and the probe's first read opens the
link, so the `finally: close()` covers both the reachable and the unreachable
case. A [setup probe](/modbus-connection/patterns/library/#a-setup-probe) is also
where you read a stable identifier — a serial number or MAC — to use as the
entry's unique id.

### Setting up the entry

Build the connection from the entry data, hand a unit to the device library, and
let the coordinator do the first read:

```python
async def async_setup_entry(hass: HomeAssistant, entry: MyConfigEntry) -> bool:
    connection = ModbusConnection(
        ModbusTcpParams(host=entry.data[CONF_HOST], port=entry.data[CONF_PORT])
    )
    entry.async_on_unload(connection.close)

    device = MyDevice(connection.for_unit(entry.data[CONF_UNIT_ID]))
    coordinator = MyCoordinator(hass, entry, device)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True
```

There is nothing to connect explicitly: the coordinator's first read establishes
the link. If the device is unreachable that read fails, and
`async_config_entry_first_refresh()` turns it into `ConfigEntryNotReady` so Home
Assistant retries setup for you.

`entry.async_on_unload(connection.close)` is the whole teardown, and it also runs
when setup fails, so register it right after constructing the connection.
`close()` is permanent — a reload builds a fresh connection rather than reviving
the old one.

:::tip[If your device needs a pause between frames]
You own the connection, so set the gap on it directly with `message_spacing`. Use
[per-unit spacing](/modbus-connection/getting-started/connection-parameters/#request-spacing)
only when your link carries several units and just one of them needs pacing.
:::

## The coordinator

A `DataUpdateCoordinator._async_update_data` becomes a one-liner: refresh the
device, and let its typed attributes back the entities.

```python
async def _async_update_data(self) -> None:
    try:
        await self.device.async_update()
    except ModbusError as err:
        raise UpdateFailed(str(err)) from err
```

Each entity's native value is then just an attribute read on the device library —
`self.coordinator.device.sensors.outside_1` — with the field metadata (`unit`,
enum members) feeding the entity's `native_unit_of_measurement`, `device_class`
and so on.

## Reconnecting is automatic

The connection re-establishes itself. Every request connects first, so the poll
after a dropped link opens a new one; a link that is down surfaces as a
`ModbusConnectionError` out of the update, the coordinator marks the entities
unavailable, and the next successful poll brings them back.

**Do not reload the config entry when the connection is lost.** A reload tears
down every entity and re-runs setup for a condition that heals itself within one
update interval, and a device that stays offline for a while turns that into
repeated setup churn. `on_connection_lost` remains available for callers that
need to observe the transport, but a device integration has no reason to reload
on it.

## Reload when the SunSpec map shifts

One condition *does* need setup to run again. Components placed at
[discovered SunSpec models](/modbus-connection/modelling/sunspec-discovery/) are
bound to the addresses that were scanned during setup. If the device rearranges
its model chain — a firmware update, an added meter — those addresses are stale.
`SunSpecComponent` catches this by verifying the model header on every update and
raising `SunSpecMapShiftError`. Reload the entry so setup rescans and rebuilds the
components at their new addresses:

```python
async def _async_update_data(self) -> None:
    try:
        await self.device.async_update()
    except SunSpecMapShiftError as err:
        self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
        raise UpdateFailed(str(err)) from err
    except ModbusError as err:
        raise UpdateFailed(str(err)) from err
```

`SunSpecMapShiftError` is **not** a `ModbusError` — it needs its own `except`
clause, or it escapes the coordinator as an unexpected exception.

## Errors map cleanly

Catch [`ModbusError`](/modbus-connection/reference/exceptions/) in the coordinator
and raise `UpdateFailed`. The neutral hierarchy means the same handling works
whichever backend the integration ships:

- `ModbusConnectionError` → the link dropped; the coordinator marks the device
  unavailable and the next poll reconnects.
- `ModbusTimeoutError` (also a builtin `TimeoutError`) → a slow or absent
  response.
- `ModbusExceptionError` → the device rejected the request (`.exception_code`).

## Diagnostics

Home Assistant lets a user **download diagnostics** for a device. For a Modbus
device the most useful payload is the raw register map — every register the
integration reads, with its raw value — so an issue report shows exactly what the
device returned. `Component`, `ComponentGroup` and `ManualComponent` all expose
`async_read_raw()` for this: it runs the same reads as `async_update()` —
including any [`repeating_group`](/modbus-connection/modelling/repeats/) second
pass — but returns the raw words and bits keyed by absolute address,
`{space: {address: value}}`, undecoded.

```python
async def async_get_config_entry_diagnostics(hass, entry):
    coordinator = entry.runtime_data
    return {
        "registers": await coordinator.device.async_read_raw(),
    }
```

`async_read_raw()` reads the device fresh, so it reflects the live register
state at download time. It raises the same
[`ModbusError`](/modbus-connection/reference/exceptions/) subclasses as an update;
catch them if you'd rather serialize a diagnostics payload than fail the download.
Its keys are the four Modbus spaces — `"holding"`, `"input"`, `"coil"`,
`"discrete"` — each an address-keyed map of raw values.

A downloaded snapshot also replays straight into the mock backend with
[`load_raw()`](/modbus-connection/reference/testing/#replaying-a-raw-snapshot), so
a raw dump attached to a bug report can back a regression test with no hardware.

## Testing without hardware

The library layer is fully testable with the shipped
[mock backend](/modbus-connection/reference/testing/) — a pytest plugin that
implements the same APIs. Your device library's tests need no Home Assistant
and no device; the integration layer then only has to test the Home Assistant
wiring.

## Checklist

- [ ] Device communication lives in a **separate PyPI library**, not the
      integration (a Core requirement).
- [ ] Device library has no Home Assistant import and is tested against the mock.
- [ ] The config flow gathers the connection details — plus the unit id when the
      user can choose it — and validates them by probing the device.
- [ ] `async_setup_entry` constructs the `ModbusConnection` and registers
      `connection.close` with `entry.async_on_unload`.
- [ ] Coordinator calls the library's `async_update()` and maps `ModbusError` to
      `UpdateFailed`.
- [ ] The entry is **not** reloaded when the connection drops — reconnection is
      automatic.
- [ ] A SunSpec integration reloads the entry on `SunSpecMapShiftError`.
- [ ] Entities read typed attributes; field `unit=` feeds entity metadata.
- [ ] Diagnostics download returns the raw register map via `async_read_raw()`.
- [ ] Read the [official Modbus integration guide](https://developers.home-assistant.io/docs/modbus/introduction).
