---
title: Integration structure
description: How modbus-connection fits a Home Assistant integration.
---

modbus-connection is a clean foundation for a **built-in Home Assistant
integration**. The split it enforces — a connection owned at the top, stateless
units handed down, typed components over them — lines up with how Home
Assistant wants a device integration structured.

:::note[Read the official guide first]
Home Assistant maintains a dedicated guide for Modbus-based integrations. Read
it alongside this page. It covers the coordinator pattern, entity setup, and
config flow in Home Assistant terms:

**[developers.home-assistant.io → Modbus integration](https://developers.home-assistant.io/docs/modbus/introduction)**
:::

## The library requirement

A built-in integration **may not talk to the device directly**. Home Assistant
Core requires all protocol and device communication to live in a **separate
library published to PyPI**. The integration itself is a thin layer that wires
that library to Home Assistant's entities, config flow and coordinator.

That requirement is exactly the
[device-object pattern](/modbus-connection/patterns/library/): a standalone
package, built on modbus-connection, that exposes a device object over
`Component`s and consumes a `ModbusUnit`. Build that library first. The hard
part — the register map — then gets tested against the
[mock](/modbus-connection/patterns/testing/) with no Home Assistant in the loop.

:::note[Custom integrations]
A custom integration is not bound by the separate-library rule; you can ship
the device code inside the integration itself. We still recommend modelling it
as its own library. It keeps the register map testable without Home Assistant,
and it is what you would need anyway to submit the integration to Core later.
:::

## The recommended layering

An integration built this way has three clear layers:

1. **modbus-connection** — the connection + modelling foundation the library is
   built on.
2. **A device library** (its own PyPI package) — the
   [device-object pattern](/modbus-connection/patterns/library/): a top-level device
   object over `Component`s, backend-neutral, consuming a `ModbusUnit`. This has
   **no Home Assistant dependency** and is released and tested on its own.
3. **Your device integration** (in `homeassistant/components/<domain>/`) — owns
   the `ModbusConnection`, gathers its connection details in its own config flow,
   hands a `ModbusUnit` to the library, and polls it from a
   `DataUpdateCoordinator`.

## The config flow

Collect the transport details for the params object your integration builds:
`CONF_HOST` / `CONF_PORT` for TCP, the serial device and baud rate for RTU.
Also ask for the **unit id where the user can choose it**. A device with a
fixed station address — common for a TCP-native device — keeps that address as
a constant in the integration instead of asking for it.

Ask only for what you cannot detect. Whether a device serves an optional
sub-system is the library's job to settle, and it does that by
[probing at setup](/modbus-connection/patterns/library/).

Validate the input by actually talking to the device. Do not open a connection
yourself: ask `modbus` for a temporary unit with `async_get_temporary_unit`.
The flow has no config entry yet to tie a hold to, so the hold lasts for the
context. If an entry already uses the device over different link settings,
entering the context raises `HomeAssistantError`.

```python
from modbus_connection import ModbusError, ModbusTcpParams

from homeassistant.components.modbus import async_get_temporary_unit


async def _async_probe(
    hass: HomeAssistant, host: str, port: int, unit_id: int
) -> tuple[str, str]:
    """Return the device's serial and model, or raise ModbusError if unreachable."""
    async with async_get_temporary_unit(
        hass, ModbusTcpParams(host=host, port=port), unit_id
    ) as unit:
        device = MyDevice(unit)
        await device.async_update()
        return device.controller.serial_number, device.controller.model


class MyConfigFlow(ConfigFlow, domain=DOMAIN):
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                serial, model = await _async_probe(
                    self.hass,
                    user_input[CONF_HOST],
                    user_input[CONF_PORT],
                    user_input[CONF_UNIT_ID],
                )
            except ModbusError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(serial)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=model, data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )
```

## Setting up the entry

A Modbus link addresses many units, and a device answers one request at a time,
so two integrations that each open their own socket to one device compete for
it. Home Assistant's `modbus` integration hands out units over connections it
shares between integrations. Ask it for one from the entry data, hand that unit
to the device library, and let the coordinator do the first read:

```python
from homeassistant.components.modbus import async_get_unit


async def async_setup_entry(hass: HomeAssistant, entry: MyConfigEntry) -> bool:
    unit = async_get_unit(
        hass,
        entry,
        ModbusTcpParams(host=entry.data[CONF_HOST], port=entry.data[CONF_PORT]),
        entry.data[CONF_UNIT_ID],
    )

    device = MyDevice(unit)
    coordinator = MyCoordinator(hass, entry, device, device.async_update, SCAN_INTERVAL)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True
```

Your config flow still gathers the connection details; you pass them here rather
than building the connection yourself. Two integrations that ask with equal
details get units over one connection, so their requests serialize behind it.

There is no teardown to register. The connection belongs to `modbus`, which
closes it when the last entry holding a unit on it unloads.

The coordinator's first read establishes the link. If the device is unreachable,
that read fails, and `async_config_entry_first_refresh()` turns the failure into
`ConfigEntryNotReady`. Home Assistant then retries setup for you.

:::note[If your device needs a pause between frames]
Set it on the unit with
[`set_message_spacing()`](/modbus-connection/connection/connections-and-units/#request-spacing).
The gap then applies to your device rather than to everything on a shared link,
which is what you want when the link carries several units and only yours needs
pacing.
:::

## The coordinator

`async_update()` returns an `UpdateReport` — which sub-systems refreshed, and
which failed — so the coordinator's data is that report:

```python
class MyCoordinator(DataUpdateCoordinator[UpdateReport]):
    """Run one of the device's update methods on its own interval."""

    _failed: frozenset[str] = frozenset()

    def __init__(
        self,
        hass: HomeAssistant,
        entry: MyConfigEntry,
        device: MyDevice,
        poll: Callable[[], Awaitable[UpdateReport]],
        interval: timedelta,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=entry.title,
            update_interval=interval,
        )
        self.device = device
        self._poll = poll

    async def _async_update_data(self) -> UpdateReport:
        try:
            report = await self._poll()
        except ModbusError as err:
            raise UpdateFailed(str(err)) from err
        if not report.updated:
            errors = list(report.failed.values())
            raise UpdateFailed(
                f"no sub-system answered: {errors[0]}"
            ) from ExceptionGroup("every sub-system failed", errors)

        for name in sorted(report.failed.keys() - self._failed):
            _LOGGER.warning("Failed to fetch %s: %s", name, report.failed[name])
        self._failed = frozenset(report.failed)
        return report

    @cached_property
    def device_info(self) -> DeviceInfo:
        """Describe the device to the registry."""
        controller = self.device.controller
        return DeviceInfo(
            identifiers={(DOMAIN, controller.serial_number)},
            manufacturer=MANUFACTURER,
            model=controller.model,
            sw_version=controller.firmware,
            serial_number=controller.serial_number,
        )
```

Each entity reads one attribute off the device, and names the sub-system it came
from:

```python
@dataclass(frozen=True, kw_only=True)
class MyDeviceSensorDescription(SensorEntityDescription):
    """Describe a sensor backed by a device attribute."""

    value_fn: Callable[[MyDevice], float | None]
    report_name: str  # the name mentioned in the update report


SENSORS: tuple[MyDeviceSensorDescription, ...] = (
    MyDeviceSensorDescription(
        key="outside_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        report_name="sensors",
        value_fn=lambda device: device.sensors.outside_1,
    ),
    MyDeviceSensorDescription(
        key="circuit_1_flow",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        # Both circuits are read as one, so the report names them "circuits".
        report_name="circuits",
        value_fn=lambda device: device.heating_circuit_1.flow,
    ),
)


class MySensor(CoordinatorEntity[MyCoordinator], SensorEntity):
    entity_description: MyDeviceSensorDescription

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.entity_description.report_name in self.coordinator.data.updated
        )

    @property
    def native_value(self) -> float | None:
        return self.entity_description.value_fn(self.coordinator.device)


class MyTotalSensor(CoordinatorEntity[MyCoordinator], RestoreSensor):
    """A long-term statistic: it holds its last value, and may outlive the device."""

    entity_description: MyDeviceSensorDescription

    @property
    def available(self) -> bool:
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last_data := await self.async_get_last_sensor_data()) is not None:
            self._attr_native_value = last_data.native_value
        self._process_data()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._process_data()
        super()._handle_coordinator_update()

    def _process_data(self) -> None:
        value = self.entity_description.value_fn(self.coordinator.device)
        if value is None:
            return
        last = self._attr_native_value
        if (
            self.entity_description.state_class is SensorStateClass.TOTAL_INCREASING
            and last is not None
            and last * 0.99 <= value < last
        ):
            return  # ignore firmware issue causing minor decrease
        self._attr_native_value = value


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        (
            MyTotalSensor
            if description.state_class
            in (SensorStateClass.TOTAL, SensorStateClass.TOTAL_INCREASING)
            else MySensor
        )(coordinator, description)
        for description in SENSORS
    )
```

An entity whose sub-system failed goes unavailable, with one exception: a
**long-term statistic**. A `TOTAL` or `TOTAL_INCREASING` sensor holds its last
value, because devices legitimately go offline — a solar inverter powers down
every night — and a gap damages long-term statistics and the energy dashboard.
[`RestoreSensor`](https://developers.home-assistant.io/docs/core/entity/sensor)
seeds it across a restart.

### Splitting the poll

Where the library
[polls settings apart from readings](/modbus-connection/patterns/library/),
construct one coordinator per poll:

```python
readings = MyCoordinator(
    hass, entry, device, device.async_update_readings, SCAN_INTERVAL
)
settings = MyCoordinator(
    hass, entry, device, device.async_update_settings, timedelta(minutes=5)
)
```

## Reconnecting is automatic

The connection re-establishes itself. Every request connects first, so the poll
after a dropped link opens a new one. A link that is down surfaces as a
`ModbusConnectionError` out of the update. The coordinator marks the entities
unavailable, and the next successful poll brings them back.

So don't reload the config entry when the connection is lost. The condition
heals itself within one update interval.

Automatic reconnection cannot see one case: a link that is **up but
unresponsive**. Some bridges keep the socket open while the device behind them
stops answering, so every poll times out against the same dead link. Most
integrations never hit this and need nothing here. If yours is known to — some
serial-to-network bridges wedge this way — call `disconnect()` once polls keep
timing out. A device built to the
[library pattern](/modbus-connection/patterns/library/) raises
`ModbusTimeoutError` only when nothing answered at all, which is exactly this
condition. A timeout it reports in the `UpdateReport` instead means the device
is answering, so the link is not wedged:

```python
async def _async_update_data(self) -> UpdateReport:
    try:
        report = await self._poll()
    except ModbusTimeoutError as err:
        self._timeouts += 1
        if self._timeouts >= 3:  # a stuck link, not a slow reply
            await self.unit.disconnect()
        raise UpdateFailed(str(err)) from err
    except ModbusError as err:
        raise UpdateFailed(str(err)) from err
    self._timeouts = 0
    ...  # report handling as above
```

The next poll establishes a fresh link over the same units and components.
Nothing is rebuilt, and the entry still is not reloaded. Hand the coordinator
the unit alongside the device for this — `disconnect()` on the unit recycles
the shared connection, so you never need to hold the connection itself.

Count in one coordinator only — the one on the fastest interval. This prevents
a second coordinator dropping the link under a poll already in flight.

## Reload when the SunSpec map shifts

One condition *does* need setup to run again. Components placed at
[discovered SunSpec models](/modbus-connection/modelling/sunspec-discovery/) are
bound to the addresses that were scanned during setup. If the device rearranges
its model chain — a firmware update, an added meter — those addresses are stale.
`SunSpecComponent` catches this by verifying the model header on every update
and raising `SunSpecMapShiftError`. Reload the entry so setup rescans and
rebuilds the components at their new addresses:

```python
async def _async_update_data(self) -> UpdateReport:
    try:
        report = await self.device.async_update()
    except SunSpecMapShiftError as err:
        self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
        raise UpdateFailed(str(err)) from err
    except ModbusError as err:
        raise UpdateFailed(str(err)) from err
    ...  # report handling as above
```

`SunSpecMapShiftError` is **not** a `ModbusError`. It needs its own `except`
clause, or it escapes the coordinator as an unexpected exception.

## Errors map cleanly

Catch [`ModbusError`](/modbus-connection/connection/reference/#exceptions) in
the coordinator and raise `UpdateFailed`. The neutral hierarchy means the same
handling works whichever backend the integration ships:

- `ModbusConnectionError` → the link dropped; the coordinator marks the device
  unavailable and the next poll reconnects.
- `ModbusTimeoutError` (also a builtin `TimeoutError`) → a slow or absent
  response.
- `ModbusExceptionError` → the device rejected the request (`.exception_code`).

## Diagnostics

Home Assistant lets a user **download diagnostics** for a device. For a Modbus
device the most useful payload is the raw register map: every register the
integration reads, with its raw value. An issue report then shows exactly what
the device returned. A `Component` exposes `async_read_raw()` for this. It runs
the same reads as `async_update()`, but returns the raw words and bits keyed by
absolute address, `{space: {address: value}}`, undecoded. Have the device merge
its components' maps into one, so diagnostics is a single call:

```python
async def async_get_config_entry_diagnostics(hass, entry):
    coordinator = entry.runtime_data
    registers = await coordinator.device.async_read_raw()
    for address in range(SERIAL_REGISTER, SERIAL_REGISTER + SERIAL_WORDS):
        registers["holding"].pop(address, None)
    return {
        "updated": coordinator.data.updated,
        "failed": {name: str(err) for name, err in coordinator.data.failed.items()},
        "registers": registers,
    }
```

`async_read_raw()` reads the device fresh, so it reflects the live register
state at download time. It raises the same
[`ModbusError`](/modbus-connection/connection/reference/#exceptions) subclasses
as an update; catch them if you'd rather serialize a diagnostics payload than
fail the download. Its keys are the four Modbus spaces — `"holding"`,
`"input"`, `"coil"`, `"discrete"` — each an address-keyed map of raw values.

A downloaded snapshot also replays straight into the mock backend with
[`load_raw()`](/modbus-connection/patterns/testing/#replaying-a-raw-snapshot).
A raw dump attached to a bug report can therefore back a regression test with
no hardware.

## Testing without hardware

The library layer is fully testable with the shipped
[mock backend](/modbus-connection/patterns/testing/), a pytest plugin that
implements the same APIs. Your device library's tests need no Home Assistant
and no device. The integration layer then only has to test the Home Assistant
wiring.

## Checklist

- [ ] Device communication lives in a **separate PyPI library**, not the
      integration (a Core requirement).
- [ ] Device library has no Home Assistant import and is tested against the mock.
- [ ] The domain is named after the device, not after the transport:
      `sofar`, not `sofar_modbus`.
- [ ] The config flow gathers the connection details and validates them by
      probing the device over a unit from `async_get_temporary_unit`. It asks
      for the unit id only when the device's address can differ; a fixed
      address is a constant in the integration. It asks nothing the library
      settles by probing.
- [ ] `async_setup_entry` asks `modbus` for the unit with `async_get_unit`.
- [ ] Coordinator returns the library's `UpdateReport`, maps `ModbusError` to
      `UpdateFailed`, and fails the update when no sub-system answered.
- [ ] Every coordinator has run `async_config_entry_first_refresh()` before the
      platforms are forwarded.
- [ ] The entry is **not** reloaded when the connection drops — reconnection is
      automatic.
- [ ] A SunSpec integration reloads the entry on `SunSpecMapShiftError`.
- [ ] Entities read typed attributes. An entity goes unavailable when its own
      sub-system is in `report.failed`.
- [ ] `TOTAL` / `TOTAL_INCREASING` sensors stay available when the device is
      offline, and restore their last value with `RestoreSensor` across a
      restart, so long-term statistics keep their history.
- [ ] Diagnostics download returns the raw register map via `async_read_raw()`,
      with the registers holding personal information dropped.
- [ ] Read the [official Modbus integration guide](https://developers.home-assistant.io/docs/modbus/introduction).
