# modbus-connection

A small, **backend-neutral** Modbus connection abstraction.

The top-level `modbus_connection` package provides the abstract
`ModbusConnection`, the `ModbusUnit`
[Protocol](https://typing.readthedocs.io/en/latest/spec/protocol.html), and a
tiny exception hierarchy. Two interchangeable backends implement them
([tmodbus](https://github.com/wlcrs/tmodbus) and
[pymodbus](https://github.com/pymodbus-dev/pymodbus)); the bare install pulls
neither.

One physical Modbus link addresses many units (1–247). Many consumers can
share a single, internally-serialized connection instead of each opening a
competing socket. This package is the connection abstraction that makes that
sharing possible while keeping the backend swappable.

## Install

```bash
pip install "modbus-connection[tmodbus]"    # tmodbus backend
pip install "modbus-connection[pymodbus]"   # pymodbus backend
```

## Example

Model a device once, then construct, update, read, and write it. The optional
`modbus_connection.model` framework maps a device's registers and coils to typed
attributes and reads the whole device in as few Modbus calls as possible.

```python
import asyncio

from modbus_connection import ModbusTcpParams
from modbus_connection.model import Component, gauge, uint32, coil
from modbus_connection.tmodbus import ModbusConnection


class Meter(Component):
    voltage = gauge(0, 0.1, unit="V")  # scaled 16-bit register
    """Grid voltage."""

    current = gauge(1, 0.1, unit="A")
    """Grid current."""

    energy = uint32(2, unit="Wh")  # 32-bit over two registers
    """Lifetime energy."""

    relay = coil(0, writable=True)
    """Load relay."""


async def main() -> None:
    conn = ModbusConnection(ModbusTcpParams(host="192.168.1.50", port=502))
    try:
        meter = Meter(conn.for_unit(1))

        await meter.async_update()  # one pooled read per space
        print(meter.voltage, meter.current, meter.energy, meter.relay)

        await meter.write("relay", True)  # write a writable field
    finally:
        await conn.close()


asyncio.run(main())
```

## Documentation

Everything else — the other transports (UDP, serial, TLS), the full field-type
and read-planning reference, repeated sub-units, the SunSpec field types and
model generator, the in-memory mock backend for tests, and the exception
hierarchy — lives on the website:

**<https://home-assistant-libs.github.io/modbus-connection/>**

## Develop

```bash
uv sync --extra tmodbus
uv run pytest
```

The suite runs both backends against a real in-process Modbus server (tmodbus's,
over TCP, UDP, RTU-over-TCP, serial and TLS) rather than against mocks, so it
covers actual framing and error responses — and running the pymodbus client
against the tmodbus server checks the two implementations against each other.
`tests/conftest.py` has the datastore and the server helpers.

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
