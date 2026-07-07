---
title: Custom integrations
description: How modbus-connection fits into a custom Home Assistant integration built on a device library.
---

modbus-connection is a clean foundation for a **custom Home Assistant
integration**. The split it enforces — a connection owned at the top, stateless
units handed down, typed components over them — lines up exactly with how Home
Assistant wants a device integration structured.

:::note[Read the official guide first]
Home Assistant maintains a dedicated guide for Modbus-based integrations. Read it
alongside this page — it covers the coordinator pattern, entity setup, and config
flow in Home Assistant terms:

**[developers.home-assistant.io → Modbus integration](https://developers.home-assistant.io/docs/modbus/introduction)**
:::

## The recommended layering

A custom integration built this way has three clear layers:

1. **A device library** (its own package) — the
   [entrypoint pattern](/modbus-connection/patterns/library/): a top-level device
   object over `Component`s, backend-neutral, consuming a `ModbusUnit`. This has
   **no Home Assistant dependency** and can be released and tested on its own.
2. **modbus-connection** — the connection + modelling foundation the library is
   built on.
3. **The Home Assistant integration** — owns the `ModbusConnection`, runs a
   `DataUpdateCoordinator` that calls the library's `async_update()`, and exposes
   entities that read the library's typed attributes.

Keeping the device library separate from the integration is the single most
valuable decision: the hard part (the register map) gets tested against the
[mock](/modbus-connection/reference/testing/) with no Home Assistant in the loop.

## Who owns the connection

In Home Assistant terms:

- The **integration** (the config entry) is the *owner*. It calls a connect
  function once on setup, holds the `ModbusConnection`, and calls `close()` on
  unload.
- The **coordinator** and **entities** are *consumers*. They only ever touch a
  `ModbusUnit` (via the device library), never the connection.

```python
# In your config entry setup (sketch — Home Assistant specifics omitted):
from modbus_connection.pymodbus import connect_tcp

connection = await connect_tcp(entry.data["host"], port=entry.data["port"])
unit = connection.for_unit(entry.data["slave"])
device = MyDevice(unit)          # your device library's entrypoint

# store `connection` on the entry so unload can close it
```

Because a connection self-reports drops through `on_connection_lost` but does not
reconnect, the integration is the natural place to recreate it — which is exactly
where Home Assistant already handles retries and availability.

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

## Testing without hardware

The library layer is fully testable with the shipped
[mock backend](/modbus-connection/reference/testing/) — a pytest plugin that
implements the same Protocols. Your device library's tests need no Home Assistant
and no device; the integration layer then only has to test the Home Assistant
wiring.

## Checklist

- [ ] Device library is a separate package with no Home Assistant import.
- [ ] The integration owns the `ModbusConnection` and closes it on unload.
- [ ] Coordinator calls the library's `async_update()` and maps `ModbusError` to
      `UpdateFailed`.
- [ ] Entities read typed attributes; field `unit=` feeds entity metadata.
- [ ] Library tested against the mock; integration tests cover the wiring.
- [ ] Read the [official Modbus integration guide](https://developers.home-assistant.io/docs/modbus/introduction).
