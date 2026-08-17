"""Every backend client is built with the timeout the caller asked for.

A timeout only takes effect if it reaches the backend's client factory, so these
pin the kwargs the factory is called with, for each transport and framing on
both backends.
"""

from __future__ import annotations

from collections.abc import Callable
from types import ModuleType
from typing import Any

import pytest

import modbus_connection.pymodbus as pymodbus_backend
import modbus_connection.tmodbus as tmodbus_backend

TIMEOUT = 3.5

# Every client factory each backend reaches for, patched wholesale so a
# transport that grows a new one still lands in the recorder.
_FACTORIES = {
    "tmodbus": (
        "create_async_ascii_client",
        "create_async_rtu_client",
        "create_async_rtu_over_tcp_client",
        "create_async_tcp_client",
        "create_async_udp_client",
    ),
    "pymodbus": (
        "AsyncModbusSerialClient",
        "AsyncModbusTcpClient",
        "AsyncModbusTlsClient",
        "AsyncModbusUdpClient",
    ),
}

_BACKENDS: dict[str, ModuleType] = {
    "pymodbus": pymodbus_backend,
    "tmodbus": tmodbus_backend,
}


class _FakeClient:
    """A stand-in client that satisfies both backends' connect and teardown."""

    connected = True

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture
def factory_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Record the kwargs the backend client factories are called with."""
    recorded: dict[str, Any] = {}

    def record(*_args: object, **kwargs: Any) -> _FakeClient:
        recorded.update(kwargs)
        return _FakeClient()

    for name, module in _BACKENDS.items():
        for factory in _FACTORIES[name]:
            monkeypatch.setattr(module, factory, record)
    return recorded


# tmodbus rejects RTU- and ASCII-over-UDP at construction, so UDP is socket-only.
CONNECTS: dict[str, Callable[[ModuleType], Any]] = {
    "tcp": lambda backend: backend.connect_tcp("127.0.0.1", timeout=TIMEOUT),
    "rtu-over-tcp": lambda backend: backend.connect_tcp(
        "127.0.0.1", framer="rtu", timeout=TIMEOUT
    ),
    "ascii-over-tcp": lambda backend: backend.connect_tcp(
        "127.0.0.1", framer="ascii", timeout=TIMEOUT
    ),
    "udp": lambda backend: backend.connect_udp("127.0.0.1", timeout=TIMEOUT),
    "tls": lambda backend: backend.connect_tls("127.0.0.1", timeout=TIMEOUT),
    "serial-rtu": lambda backend: backend.connect_serial("/dev/null", timeout=TIMEOUT),
    "serial-ascii": lambda backend: backend.connect_serial(
        "/dev/null", framer="ascii", timeout=TIMEOUT
    ),
}


@pytest.mark.parametrize("backend", list(_BACKENDS), ids=list(_BACKENDS))
@pytest.mark.parametrize("transport", list(CONNECTS), ids=list(CONNECTS))
async def test_timeout_reaches_the_client(
    factory_kwargs: dict[str, Any], backend: str, transport: str
) -> None:
    """No transport drops the timeout between the factory and the client."""
    conn = await CONNECTS[transport](_BACKENDS[backend])
    try:
        assert factory_kwargs["timeout"] == TIMEOUT
    finally:
        await conn.close()
