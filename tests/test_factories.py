"""The eager connect factories: immediate initial connect, then self-healing.

Unlike direct ``ModbusConnection(params)`` construction, ``connect_*`` performs
the first connect before returning — an unreachable device raises from the
factory call itself. The returned object is the same lazy connection, so the
reconnect semantics afterwards are identical.
"""

from __future__ import annotations

import socket

import pytest

import modbus_connection.pymodbus as pymodbus_backend
import modbus_connection.tmodbus as tmodbus_backend
from modbus_connection import ModbusConnection, ModbusConnectionError

from .conftest import UNIT_ID, drop_link

both_backends = pytest.mark.parametrize(
    "backend",
    [pymodbus_backend, tmodbus_backend],
    ids=["pymodbus", "tmodbus"],
)


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@both_backends
async def test_connect_tcp_connects_immediately(
    modbus_server: tuple[str, int], backend: object
) -> None:
    host, port = modbus_server
    conn = await backend.connect_tcp(host, port=port)  # type: ignore[attr-defined]
    try:
        assert conn.connected is True  # already live, no request needed
        assert isinstance(conn, ModbusConnection)
        assert await conn.for_unit(UNIT_ID).read_holding_registers(0, 1) == [1234]
    finally:
        await conn.close()


@both_backends
async def test_connect_tcp_unreachable_raises_immediately(backend: object) -> None:
    # Nothing is listening on this port: the factory raises the neutral error
    # itself instead of returning a connection that fails later.
    with pytest.raises(ModbusConnectionError):
        await backend.connect_tcp("127.0.0.1", port=_closed_port())  # type: ignore[attr-defined]


async def test_tmodbus_connect_udp_rejected() -> None:
    """tmodbus ships no UDP transport: connect_udp surfaces the constructor error."""
    with pytest.raises(TypeError, match="pymodbus.ModbusConnection"):
        await tmodbus_backend.connect_udp("127.0.0.1", port=502)


async def test_tmodbus_connect_tcp_ascii_rejected() -> None:
    """tmodbus has no ASCII-over-TCP transport: framer="ascii" is rejected."""
    with pytest.raises(ValueError, match="pymodbus.ModbusConnection"):
        await tmodbus_backend.connect_tcp("127.0.0.1", framer="ascii")


@both_backends
async def test_factory_connection_self_heals(
    modbus_server: tuple[str, int], backend: object
) -> None:
    # After the initial connect, factory users get the lazy semantics: a closed
    # link is re-established by the next request.
    host, port = modbus_server
    conn = await backend.connect_tcp(host, port=port)  # type: ignore[attr-defined]
    try:
        await drop_link(conn)
        assert conn.connected is False
        assert await conn.for_unit(UNIT_ID).read_holding_registers(0, 1) == [1234]
        assert conn.connected is True
    finally:
        await conn.close()


def test_old_connection_class_names_are_aliases() -> None:
    # Pre-3.8 code imported the concrete connection classes under these names.
    assert tmodbus_backend.TmodbusConnection is tmodbus_backend.ModbusConnection
    assert pymodbus_backend.PymodbusConnection is pymodbus_backend.ModbusConnection
