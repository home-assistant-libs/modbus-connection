---
title: Installation
description: Install modbus-connection with the pymodbus or tmodbus backend.
---

modbus-connection requires **Python 3.12 or newer**.

The top-level package is a pure interface and imports no Modbus library, so a
bare install pulls **no** backend. Pick one with an extra — each provides a
[`ModbusConnection`](/modbus-connection/getting-started/connections-and-units/)
and eager `connect_*` factories to connect with:

```bash
pip install "modbus-connection[tmodbus]"    # tmodbus backend
pip install "modbus-connection[pymodbus]"   # pymodbus backend
```

You can install both extras and choose per connection — the params dataclasses
and the `ModbusConnection` / `ModbusUnit` Protocols are identical across them.
See [Which backend?](/modbus-connection/getting-started/connections-and-units/#which-backend)
for the transport matrix: tmodbus reports protocol errors more precisely, while
pymodbus additionally covers Modbus UDP and ASCII-over-TCP.

## Verifying the install

```python
import modbus_connection

print(modbus_connection.__all__)
# ['ModbusConnection', 'ModbusConnectionError', 'ModbusError', ...]
```

Importing `modbus_connection` never imports a backend. A backend is only loaded
when you import `modbus_connection.tmodbus` or `modbus_connection.pymodbus`.

## For contributors

The project uses [uv](https://docs.astral.sh/uv/):

```bash
uv sync
uv run pytest
```

Formatting and linting is [ruff](https://docs.astral.sh/ruff/), enforced in CI.
Install the commit hook with [prek](https://github.com/j178/prek):

```bash
uvx prek install          # set up the git hook
uvx prek run --all-files  # format + lint everything now
```

The documentation site in `docs/` is a separate
[Astro Starlight](https://starlight.astro.build/) project:

```bash
cd docs
npm install
npm run dev      # serve locally
npm run build    # build the static site to ./dist
```
