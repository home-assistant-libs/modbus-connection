---
title: Query helper
description: Build a standalone CLI that reads a real device once and prints every value, using the modbus_connection.cli_helper building blocks.
---

A **query helper** is a small standalone script that connects to a real device,
reads it once, and dumps every value to the terminal. It's the single most useful
tool when bringing up a new device library — you can check a physical controller
without any application around it.

You don't have to hand-roll the plumbing. The library ships the building blocks in
**`modbus_connection.cli_helper`**, so a query script imports the pieces it needs
instead of re-implementing argument parsing, connection setup, read counting and
value printing every time:

| Building block | What it does |
| --- | --- |
| `add_connection_args(parser, connections=…)` | Add the connection arguments (target, transport, framer, port, timeout, serial/TLS options) to an `argparse` parser. |
| `connect_from_args(args, *, message_spacing=0.0)` | Open the connection those arguments describe (over whichever backend is installed). |
| `CountingUnit` | Wrap a `ModbusUnit` to count the block reads an update performs. |
| `print_component(component, *, title=None, file=None)` | Print every field on a component by reflection. |
| `field_rows(component)` | The `(name, value)` rows behind `print_component`, if you want to format them yourself. |

:::note[Backend]
Only `connect_from_args` needs a backend. It prefers **tmodbus** where supported,
uses **pymodbus** for UDP and ASCII-over-TCP, and falls back to pymodbus when
tmodbus is absent. With no installed backend covering the request it raises
`ModbusError`. The counter and printer are backend-neutral, so `--help` and
argument parsing work without either backend.
:::

## A complete query script

That's the whole thing — parse, connect, wrap, read, print:

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

from my_device import MyDevice  # your modelled Component / device entrypoint


async def main() -> int:
    parser = argparse.ArgumentParser(description="Query a device and print values.")
    add_connection_args(parser)
    # The unit id is not part of connecting — it varies per device and per tool —
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

Run it against a device — `add_connection_args` gives you a full connection CLI
for free:

```bash
python query.py 192.168.1.50 --unit 246 --framer rtu
python query.py /dev/ttyUSB0 --transport serial --unit 246 --baudrate 19200
python query.py --help          # works without a backend installed
```

## Through an ESPHome serial proxy

The machine running the query helper doesn't need a serial port of its own. An
ESPHome device running the [`serial_proxy`](https://esphome.io/components/serial_proxy/)
component exposes a UART over its native API, so a Modbus device wired to the ESP is
reachable over the network. Point the **serial** transport at it with an `esphome://`
URL in place of a device path — nothing else about the script changes:

```bash
python query.py "esphome://basement.local/?port_name=modbus" \
    --transport serial --unit 246 --baudrate 19200
```

The URL is just the connection target, so the serial options carry through to the
ESP's UART — `--baudrate` / `--parity` / `--stopbits` are applied remotely by the
proxy. RTU is the serial default, so no `--framer` is needed.

:::note[Requires the tmodbus backend + aioesphomeapi]
`esphome://` is resolved by **tmodbus**'s serial layer, so it works only when the
tmodbus backend is the one in use (`connect_from_args` tries it first) and
[`aioesphomeapi`](https://github.com/esphome/aioesphomeapi) is installed:
`pip install aioesphomeapi`. The pymodbus backend has no ESPHome handler and
rejects the scheme. Your query script is unchanged either way — it's the same
serial path, just a different target URL.
:::

**URL format** — `esphome://<host>[:<port>]/?port_name=<name>`, where `<port>`
defaults to `6053` (the ESPHome API port) and `<name>` is the `serial_proxy`
instance's name. For an encrypted or password-protected device, add a `noise_psk=`
(or `password=`) query parameter. A device with a single unnamed proxy also accepts
the numeric form `esphome://<host>/<instance>`.

## The building blocks

### `add_connection_args`

Adds the connection-specifying arguments in their own **"Modbus connection"**
group (plus serial and TLS groups when those transports are offered), so they read
as a block in `--help` and stay clear of your CLI's own options — like the
`--unit` you add yourself.

By default it offers every transport and framing. Pass `connections=` the
`(transport, framer)` pairs your device actually supports and the CLI narrows to
match — a device that only speaks RTU-over-TCP needs no serial, TLS, `--transport`
or `--framer` clutter:

```python
# Only RTU-over-TCP: no --transport flag, --framer fixed to rtu, no serial/TLS args.
add_connection_args(parser, connections=(("tcp", "rtu"),))
```

A `None` framer means the backend default (and is required for TLS). The parser it
produces is read back by `connect_from_args`, so the two always stay in step.

### `connect_from_args`

Opens the connection the parsed arguments describe. It prefers tmodbus where
supported, routes UDP and ASCII-over-TCP to pymodbus, and falls back to pymodbus
when tmodbus is absent. Backends are resolved lazily, so importing the module
needs no backend. Pass `message_spacing=` for a device that needs a gap between
frames — it's a fixed device property, so the tool sets it rather than exposing
it as a CLI argument:

```python
conn = await connect_from_args(args, message_spacing=0.1)
```

The returned connection is already connected. It raises `ModbusError` if no
installed backend supports the request and `ModbusConnectionError` immediately
if the link can't be opened.

### `CountingUnit`

Wrap `connection.for_unit(id)` in a `CountingUnit` before handing it to a
component. Its `reads` attribute then tallies every block read the update issued —
a quick sanity check that your [`ranges`](/modbus-connection/modelling/overview/#readable-address-ranges)
and `max_gap` are collapsing fields into as few Modbus round-trips as the plan
allows. It implements `ModbusUnit` in full, so it drops in wherever one is
expected with **no cast**:

```python
counting = CountingUnit(conn.for_unit(args.unit))
device = MyDevice(counting)
await device.async_update()
print(counting.reads)  # e.g. 6
```

### `print_component` and `field_rows`

`print_component` walks a component's public attributes by reflection and prints
each modelled field — register/coil/discrete fields and computed `@property`
values — under a heading, values aligned, with each field's `unit` appended. A new
field shows up with no change to the script. Read the component first; unread
fields render as `—`:

```python
print_component(device.sensors, title="Sensors")
```

If you want to format the output yourself (JSON, a table, grouping by section),
`field_rows(component)` returns the `(name, value)` rows and you take it from
there.

## Sample output

```text
Sensors
-------
  outside_1  4.2 °C
  flow_1     58.1 °C
  return_1   41.0 °C

6 Modbus reads
```

The read count is the payoff of [pooled planning](/modbus-connection/modelling/overview/#reads-are-pooled-into-blocks)
— dozens of fields read in a handful of Modbus round-trips. If you model your
device as a [`ComponentGroup`](/modbus-connection/modelling/component-group/),
loop over its components and `print_component` each one.
