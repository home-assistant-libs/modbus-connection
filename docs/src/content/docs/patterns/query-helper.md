---
title: Query helper
description: A standalone command-line script that connects to a real device, reads it once, and prints every value — no application required.
---

A **query helper** is a small standalone script that connects to a real device,
reads it once, and dumps every value to the terminal. It's the single most useful
tool when bringing up a new device library — you can check a physical controller
without any application around it. This page shows how one comes together, based
on the `script/query.py` in
[`trovis-modbus`](https://github.com/Tom-Bom-badil/trovis-modbus).

## Parsing transports

Use `argparse` sub-commands so one script covers both TCP and serial, with the
shared options (like `--unit`) available on each:

```python
import argparse


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Query a device and print values.")
    sub = parser.add_subparsers(dest="transport", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--unit", type=int, default=1, help="Modbus unit id")

    tcp = sub.add_parser("tcp", parents=[common], help="connect over Modbus TCP")
    tcp.add_argument("host")
    tcp.add_argument("--port", type=int, default=502)
    tcp.add_argument("--framer", choices=("rtu", "socket"), default="socket")

    serial = sub.add_parser("serial", parents=[common], help="connect over serial")
    serial.add_argument("device", help="e.g. /dev/ttyUSB0")
    serial.add_argument("--baudrate", type=int, default=19200)

    return parser.parse_args(argv)
```

## Opening the connection lazily

Import the backend **inside** the open function, not at module top level, so
`--help` works even without a backend installed and the script stays
backend-agnostic until it actually connects:

```python
from modbus_connection import ModbusConnection


async def _open(args) -> ModbusConnection:
    # Imported here so the module loads (and --help works) without a backend.
    from modbus_connection.pymodbus import connect_serial, connect_tcp

    if args.transport == "serial":
        return await connect_serial(args.device, baudrate=args.baudrate)
    return await connect_tcp(args.host, port=args.port, framer=args.framer)
```

## Counting the Modbus reads

Wrapping the `ModbusUnit` in a tiny counting proxy tells you how well the pooled
read plan collapses your fields — a great sanity check that your ranges and
`max_gap` are doing their job. It implements the read methods, delegates the rest
with `__getattr__`, and satisfies `ModbusUnit` structurally:

```python
from modbus_connection import ModbusUnit


class _CountingUnit:
    """Wraps a ModbusUnit to count the reads it performs."""

    def __init__(self, unit: ModbusUnit) -> None:
        self._unit = unit
        self.reads = 0

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        self.reads += 1
        return await self._unit.read_holding_registers(address, count)

    async def read_input_registers(self, address: int, count: int) -> list[int]:
        self.reads += 1
        return await self._unit.read_input_registers(address, count)

    async def read_coils(self, address: int, count: int) -> list[bool]:
        self.reads += 1
        return await self._unit.read_coils(address, count)

    async def read_discrete_inputs(self, address: int, count: int) -> list[bool]:
        self.reads += 1
        return await self._unit.read_discrete_inputs(address, count)

    def __getattr__(self, name: str) -> object:
        return getattr(self._unit, name)
```

## Printing every field by reflection

You don't have to hand-list every attribute. Walk the component's public
attributes, skip methods, and read the [field metadata](/modbus-connection/modelling/fields/)
(the `unit`) off the descriptor to annotate values:

```python
import inspect
from enum import IntEnum

from modbus_connection.model import Component, RegisterField


def _format(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, IntEnum):
        return value.name.lower()
    return str(value)


def _values(component: Component) -> list[tuple[str, str, str]]:
    """(name, value, unit) rows for a sub-system."""
    rows = []
    cls = type(component)
    for name in dir(component):
        if name.startswith("_"):
            continue
        static = inspect.getattr_static(cls, name, None)
        # Skip methods/coroutines; keep field descriptors, properties, constants.
        if callable(static) and not isinstance(static, property):
            continue
        value = getattr(component, name)
        if callable(value):
            continue
        unit = static.unit or "" if isinstance(static, RegisterField) else ""
        rows.append((name, _format(value), unit))
    return rows
```

## Tying it together

Connect, wrap the unit in the counter, read the device once, print, and report the
timing and read count:

```python
import asyncio
import sys
import time
from typing import cast

from modbus_connection import ModbusError, ModbusUnit


async def _run(args) -> int:
    try:
        connection = await _open(args)
    except ModbusError as err:
        print(f"Could not connect: {err}", file=sys.stderr)
        return 1

    counting = _CountingUnit(connection.for_unit(args.unit))
    try:
        device = Trovis557x(cast(ModbusUnit, counting))
        start = time.monotonic()
        await device.async_update()
        elapsed = time.monotonic() - start
    except ModbusError as err:
        print(f"Error reading device: {err}", file=sys.stderr)
        return 1
    finally:
        await connection.close()

    _print(device)
    print(f"\nQueried in {elapsed * 1000:.0f} ms ({counting.reads} Modbus reads)")
    return 0


def main() -> int:
    return asyncio.run(_run(_parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
```

Run it against a device:

```bash
python query.py tcp 192.168.1.50 --unit 246 --framer rtu
python query.py serial /dev/ttyUSB0 --unit 246 --baudrate 19200
```

```text
Sensors
-------
  outside_1  4.2 °C
  flow_1     58.1 °C
  return_1   41.0 °C

Queried in 84 ms (6 Modbus reads)
```

The read count at the end is the payoff of pooled planning — dozens of fields read
in a handful of Modbus round-trips.
