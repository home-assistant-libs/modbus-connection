# modbus-connection

A small, **backend-neutral** Modbus connection abstraction.

The top-level `modbus_connection` package is a pure interface — the
`ModbusConnection` / `ModbusUnit` [Protocols](https://typing.readthedocs.io/en/latest/spec/protocol.html)
and a tiny exception hierarchy — so consumers can type against it without
committing to a backend. Two interchangeable backends implement it
([pymodbus](https://github.com/pymodbus-dev/pymodbus) and
[tmodbus](https://github.com/wlcrs/tmodbus)); the bare install pulls neither.

One physical Modbus link addresses many units (1–247). Sharing a single,
internally-serialized connection across many consumers is strictly better than
each opening a competing socket. This package is the connection abstraction that
makes that sharing possible while keeping the backend swappable.

## Install

```bash
pip install "modbus-connection[pymodbus]"   # pymodbus backend
pip install "modbus-connection[tmodbus]"    # tmodbus backend
```

## Example

Model a device once, then connect, update, read, and write it. The optional
`modbus_connection.model` framework maps a device's registers and coils to typed
attributes and reads the whole device in as few Modbus calls as possible.

```python
import asyncio

from modbus_connection.tmodbus import connect_tcp
from modbus_connection.model import Component, gauge, uint32, coil


class Meter(Component):
    voltage = gauge(0, 0.1, unit="V")     # scaled 16-bit register
    current = gauge(1, 0.1, unit="A")
    energy = uint32(2, unit="Wh")         # 32-bit over two registers
    relay = coil(0, writable=True)


async def main() -> None:
    conn = await connect_tcp("192.168.1.50", port=502)
    try:
        meter = Meter(conn.for_unit(1))

        await meter.async_update()        # one pooled block read
        print(meter.voltage, meter.current, meter.energy, meter.relay)

        await meter.write("relay", True)  # write a writable field
    finally:
        await conn.close()


asyncio.run(main())
```

## Documentation

Everything else — the other transports (UDP, serial, TLS), the full field-type
and read-planning reference, repeated sub-units, the query helper, the in-memory
mock backend for tests, and the exception hierarchy — lives on the website:

**<https://home-assistant-libs.github.io/modbus-connection/>**

## Develop

```bash
uv sync --extra pymodbus
uv run pytest
```

Formatting/linting is [ruff](https://docs.astral.sh/ruff/) and type-checking is
[mypy](https://mypy-lang.org/), both enforced in CI. Run them locally with:

```bash
uv run mypy
```

Install the commit hook with [prek](https://github.com/j178/prek) so code is
formatted on commit:

```bash
uvx prek install          # set up the git hook
uvx prek run --all-files  # format + lint everything now
```
