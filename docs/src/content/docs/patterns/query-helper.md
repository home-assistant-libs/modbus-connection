---
title: Query helper
description: Build a standalone CLI that reads a real device once and prints every value, using the modbus_connection.cli_helper building blocks.
---

A query helper is a small standalone script. It connects to a real device,
reads it once, and prints every value to the terminal. Use it when bringing up
a new device library: it checks a physical controller without any application
around it.

```text
$ python query.py 192.168.1.50 --unit 246 --framer rtu

Sensors
-------
  outside_1  4.2 °C
  flow_1     58.1 °C
  return_1   41.0 °C

6 Modbus reads
```

The read count shows
[pooled planning](/modbus-connection/modelling/reading/#reads-are-pooled-into-blocks)
at work: dozens of fields read in a handful of Modbus round-trips.

The library ships the building blocks in `modbus_connection.cli_helper`. A
query script imports the pieces it needs instead of re-implementing argument
parsing, connection setup, read counting and value printing every time:

| Building block | Purpose |
| --- | --- |
| `add_connection_args(parser, connections=…)` | Add the connection arguments (target, transport, framer, port, timeout, serial/TLS options) to an `argparse` parser. |
| `connect_from_args(args, *, message_spacing=None)` | Open the connection those arguments describe (over whichever backend is installed). |
| `CountingUnit` | Wrap a `ModbusUnit` to count the block reads an update performs. |
| `print_component(component, *, title=None, file=None, indent="")` | Print every field on a component, and each repeating group's instances, by reflection. |
| `field_rows(component)` | The `(name, value)` rows behind `print_component`, if you want to format them yourself. |
| `group_rows(component)` | The `(name, instances)` pairs for each `repeating_group` on the component, for the same reason. |

Only `connect_from_args` needs a backend. Argument parsing, `--help`, read
counting, and printing work without one.

## A complete query script

The whole script is: parse, connect, wrap, read, print.

```python
import argparse
import asyncio

from modbus_connection import ModbusError
from modbus_connection.cli_helper import (
    CountingUnit,
    add_connection_args,
    connect_from_args,
    print_component,
)

from my_device import MyDevice  # your modelled Component / device object


async def main() -> int:
    parser = argparse.ArgumentParser(description="Query a device and print values.")
    add_connection_args(parser)
    # The unit id is not part of connecting. It varies per device and per tool,
    # so add whatever the CLI needs alongside the connection arguments.
    parser.add_argument("--unit", type=int, default=1, help="Modbus unit id")
    args = parser.parse_args()

    try:
        conn = await connect_from_args(args)
    except ModbusError as err:
        print(f"Could not connect: {err}")
        return 1

    counting = CountingUnit(conn.for_unit(args.unit))
    try:
        device = MyDevice(counting)
        await device.async_update()
    finally:
        await conn.close()

    print_component(device)
    print(f"\n{counting.reads} Modbus reads")
    return 0


raise SystemExit(asyncio.run(main()))
```

Run it against a device. `add_connection_args` provides a full connection CLI:

```bash
python query.py 192.168.1.50 --unit 246 --framer rtu
python query.py /dev/ttyUSB0 --transport serial --unit 246 --baudrate 19200
python query.py --help          # works without a backend installed
```

## Through an ESPHome serial proxy

An ESPHome device running a
[serial proxy](https://esphome.io/projects/?type=serial) can expose its
UART to the query helper. Pass an `esphome://` URL as the serial target:

```bash
python query.py "esphome://basement.local/?port_name=modbus" \
    --transport serial --unit 246 --baudrate 19200
```

This requires the `tmodbus` extra and
[`aioesphomeapi`](https://github.com/esphome/aioesphomeapi). Serial options such
as `--baudrate`, `--parity`, and `--stopbits` are applied to the remote UART.

The URL format is
`esphome://<host>[:<port>]/?port_name=<name>`. The port defaults to `6053`.
Add `noise_psk=` or `password=` for an authenticated device. A single unnamed
proxy also accepts `esphome://<host>/<instance>`.

Percent-encode the `noise_psk` or `password` value. A base64 key can contain
`+`, and a URL reads `+` as a space. Write `+` as `%2B`.

## Dumping the registers undecoded

A wrong field decodes to a plausible value. A 32-bit number read in the wrong
word order is still a number. A register that two parts of a spec describe
differently decodes to whichever reading the model took. Neither problem shows
up in the printed value.

Add a `--raw` flag that prints the registers as the device returned them:

```python
parser.add_argument(
    "--raw", action="store_true", help="also dump every register read as JSON"
)
...
if args.raw:
    print(json.dumps(await device.async_read_raw(), indent=2, sort_keys=True))
```

An issue can then quote the registers instead of the values you decoded from
them. [`load_raw`](/modbus-connection/patterns/testing/#replaying-a-raw-snapshot)
loads the dump into the mock, so the report becomes a test.

## The building blocks

### `add_connection_args`

Adds the connection-specifying arguments in their own "Modbus connection"
group, plus serial and TLS groups when those transports are offered. They read
as a block in `--help` and stay clear of your CLI's own options, such as the
`--unit` you add yourself.

By default it offers native Modbus TCP and RTU on a serial line, which is
what a device on RS-485 needs. The serial target reaches a local adapter or a
[serial server](/modbus-connection/connection/connections-and-units/#a-serial-line-reached-over-the-network),
and the TCP one a Modbus gateway:

```bash
python query.py /dev/ttyUSB0 --unit 246 --baudrate 19200
python query.py socket://192.168.1.50:8899 --unit 246 --baudrate 19200
python query.py 192.168.1.50 --transport tcp --unit 246
```

Pass `connections=` the `(transport, framer)` pairs your device supports,
most-used first, to narrow that or to offer UDP, TLS or ASCII.
`--transport` defaults to the first pair, and the CLI narrows to match. A
device that only speaks native Modbus TCP then needs no `--transport` or
serial options:

```python
add_connection_args(parser, connections=(("tcp", None),))
```

A `None` framer means the backend default (and is required for TLS).
`connect_from_args` reads the parser this produces, so the two always stay in
step.

### `connect_from_args`

Opens the connection the parsed arguments describe. Backends are resolved
lazily, so importing the module needs no backend. A device that needs a gap
between frames asks for it on its
[unit](/modbus-connection/connection/connections-and-units/#request-spacing).

```python
conn = await connect_from_args(args)
```

The returned connection is already connected. It raises `ModbusError` if it
cannot select an installed implementation and `ModbusConnectionError` if the
link cannot be opened.

### `CountingUnit`

Wrap `connection.for_unit(id)` in a `CountingUnit` before handing it to a
component. Its `reads` attribute then tallies every block read the update
issued. That is a quick check that your
[readable ranges](/modbus-connection/modelling/reading/#readable-address-ranges)
and `max_gap` collapse fields into as few Modbus round-trips as the plan
allows. It implements `ModbusUnit` in full, so it drops in wherever one is
expected, with no cast:

```python
counting = CountingUnit(conn.for_unit(args.unit))
device = MyDevice(counting)
await device.async_update()
print(counting.reads)  # e.g. 6
```

### `print_component`, `field_rows` and `group_rows`

`print_component` walks a component's public attributes by reflection and
prints each modelled field under a heading. It covers register, coil and
discrete fields and computed `@property` values. Values are aligned, with each
field's `unit` appended. A new field shows up with no change to the script:

```python
print_component(device.sensors, title="Sensors")
```

An `IntEnum` field prints as its member name, lowercased (`running`). A
[`flags()`](/modbus-connection/modelling/fields/#enum-and-flag-fields) field
prints the names of the bits it has set, joined by `|`
(`over_temperature|sensor_fault`), or `none` when nothing is set. An `IntFlag`
keeps bits its type does not name, so any leftover is appended as hex
(`low_flow|0x80`) rather than dropped. A status or fault word must not hide a
set bit.

If you model your device as a
[`ComponentGroup`](/modbus-connection/modelling/component-group/), loop over its
components and `print_component` each one. To format the output yourself (JSON,
a table, grouping by section), `field_rows(component)` returns the
`(name, value)` rows and `group_rows(component)` the `(name, instances)` pairs
for its repeating groups. Recurse into the instances with `field_rows` and
take it from there.
