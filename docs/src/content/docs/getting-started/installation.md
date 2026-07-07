---
title: Installation
description: Install modbus-connection with the pymodbus or tmodbus backend.
---

modbus-connection requires **Python 3.12 or newer**.

The top-level package is a pure interface and imports no Modbus library, so a
bare install pulls **neither** backend. Pick one with an extra:

```bash
pip install "modbus-connection[pymodbus]"   # pymodbus backend
pip install "modbus-connection[tmodbus]"    # tmodbus backend
```

You can install both extras if you want to choose the backend at runtime — the
`ModbusConnection` / `ModbusUnit` Protocols are identical across them.

## Which backend?

Both backends implement the same Protocols, so device code typed against
`ModbusUnit` runs unchanged over either. They differ only in transport coverage
and a few edge behaviours:

| | pymodbus | tmodbus |
| --- | --- | --- |
| TCP (native / RTU-over-TCP / ASCII-over-TCP) | ✅ all three | RTU-over-TCP only |
| UDP | ✅ | ❌ (raises `NotImplementedError`) |
| Serial (RTU / ASCII) | ✅ | ✅ |
| TLS (Modbus Security) | ✅ | ✅ |
| Native `message_spacing` | emulated by this package | native (`wait_between_requests`) |
| Distinguishes a garbled reply from a missing one | ❌ (both become a timeout) | ✅ (`ModbusProtocolError`) |

If you need UDP or ASCII-over-TCP, use pymodbus. Otherwise the choice is yours;
tmodbus reports protocol errors more precisely.

## Verifying the install

```python
import modbus_connection

print(modbus_connection.__all__)
# ['ModbusConnection', 'ModbusConnectionError', 'ModbusError', ...]
```

Importing `modbus_connection` never imports a backend. The backend is only
loaded when you import `modbus_connection.pymodbus` or `modbus_connection.tmodbus`.

## For contributors

The project uses [uv](https://docs.astral.sh/uv/):

```bash
uv sync --extra pymodbus
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
