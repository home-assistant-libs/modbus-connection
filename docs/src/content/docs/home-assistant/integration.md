---
title: Integration structure
description: How modbus-connection fits a Home Assistant integration, whose device logic must live in a separate PyPI library.
---

modbus-connection is a clean foundation for a **built-in Home Assistant
integration** — one that ships in Home Assistant Core. The split it enforces — a
connection owned at the top, stateless units handed down, typed components over
them — lines up exactly with how Home Assistant wants a device integration
structured.

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

An integration built this way has four clear layers:

1. **A device library** (its own PyPI package) — the
   [entrypoint pattern](/modbus-connection/patterns/library/): a top-level device
   object over `Component`s, backend-neutral, consuming a `ModbusUnit`. This has
   **no Home Assistant dependency** and is released and tested on its own.
2. **modbus-connection** — the connection + modelling foundation the library is
   built on.
3. **Home Assistant's `modbus_connection` integration** — a shared integration in
   Core that owns the actual `ModbusConnection` and hands out `ModbusUnit`s.
4. **Your device integration** (in `homeassistant/components/<domain>/`) — gets a
   `ModbusUnit` from the shared integration, runs a `DataUpdateCoordinator` that
   calls the library's `async_update()`, and exposes entities that read the
   library's typed attributes.

Keeping the device library separate is not just good practice here — it is a
condition for merging into Core, and it means the hard part (the register map)
gets tested against the [mock](/modbus-connection/reference/testing/) with no Home
Assistant in the loop.

## Who owns the connection

**Your integration does not own the connection.** In Home Assistant, a shared
**`modbus_connection` integration** owns the `ModbusConnection` and manages its
lifecycle. Your device integration depends on it and only ever obtains a
**`ModbusUnit`** for its configured unit id, which it passes straight into your
device library:

```python
from homeassistant.components.modbus_connection import async_get_unit

async def async_setup_entry(hass, entry) -> bool:
    # CONF_CONNECTION is the modbus_connection config entry id; CONF_UNIT_ID the
    # station address. async_get_unit raises ConnectionNotReady (a
    # ConfigEntryNotReady subclass) if the shared link isn't up yet — Home
    # Assistant then retries setup for you.
    unit = async_get_unit(
        hass, entry.data[CONF_CONNECTION], int(entry.data[CONF_UNIT_ID])
    )
    device = MyDevice(unit)          # your device library's entrypoint

    # The connection does not self-reconnect: when the shared link drops, reload
    # this entry so setup re-runs and picks up a fresh unit once it is back.
    entry.async_on_unload(
        unit.on_connection_lost(
            lambda: hass.config_entries.async_schedule_reload(entry.entry_id)
        )
    )

    coordinator = MyCoordinator(hass, entry, device)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    ...
```

Your config flow stores just those two values — the connection entry id and the
unit id — not a host or port; the connection details live in the shared
`modbus_connection` entry.

`unit.on_connection_lost` returns an unsubscribe, so registering it with
`entry.async_on_unload` detaches the callback automatically when the entry
unloads.

This is the whole point of the connection / unit split: **one physical Modbus link
is shared across every device integration on it**, rather than each opening a
competing socket. You never call a connect function, never hold the connection,
and never call `close()` — the shared integration does all of that. You work
entirely in terms of the `ModbusUnit` you were handed, exactly as the device
library does.

:::tip[If your device needs a pause between frames]
Connection-wide `message_spacing` is set by the connection owner, which you are
not — and forcing it on the shared link would slow every other device on it. When
only *your* device needs the gap, pace your own unit instead:

```python
unit.set_message_spacing(0.05)   # ≥50 ms between requests to this unit
```

The gap is keyed by unit id, so it holds across the shared connection no matter
which handle issues the request. See
[Per-unit spacing](/modbus-connection/getting-started/connections-and-units/#per-unit-spacing).
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

## Errors map cleanly

Catch [`ModbusError`](/modbus-connection/reference/exceptions/) in the coordinator
and raise `UpdateFailed`. The neutral hierarchy means the same handling works
whichever backend the integration ships:

- `ModbusConnectionError` → the link dropped; let the coordinator mark the device
  unavailable and the integration recreate the connection.
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
Its keys are the register spaces the device serves — `"holding"`, `"input"`,
`"coil"`, `"discrete"` — each an address-keyed map of raw values.

## Testing without hardware

The library layer is fully testable with the shipped
[mock backend](/modbus-connection/reference/testing/) — a pytest plugin that
implements the same Protocols. Your device library's tests need no Home Assistant
and no device; the integration layer then only has to test the Home Assistant
wiring.

## Checklist

- [ ] Device communication lives in a **separate PyPI library**, not the
      integration (a Core requirement).
- [ ] Device library has no Home Assistant import and is tested against the mock.
- [ ] The integration gets its `ModbusUnit` from the shared `modbus_connection`
      integration via `async_get_unit` — it never opens or closes a connection.
- [ ] Coordinator calls the library's `async_update()` and maps `ModbusError` to
      `UpdateFailed`.
- [ ] Entities read typed attributes; field `unit=` feeds entity metadata.
- [ ] Diagnostics download returns the raw register map via `async_read_raw()`.
- [ ] Read the [official Modbus integration guide](https://developers.home-assistant.io/docs/modbus/introduction).
