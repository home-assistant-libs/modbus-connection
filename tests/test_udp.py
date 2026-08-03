"""ModbusUdpParams talks Modbus over UDP on both backends' clients."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import ModuleType

import pytest

import modbus_connection.pymodbus as pymodbus_backend
import modbus_connection.tmodbus as tmodbus_backend
from modbus_connection import ModbusUdpParams
from modbus_connection._client import BaseModbusConnection

from .modbus_server import holding_store, serve_udp

UNIT_ID = 1

backends = pytest.mark.parametrize(
    "client_cls",
    [pymodbus_backend.ModbusConnection, tmodbus_backend.ModbusConnection],
    ids=["pymodbus", "tmodbus"],
)

backend_modules = pytest.mark.parametrize(
    "backend",
    [pymodbus_backend, tmodbus_backend],
    ids=["pymodbus", "tmodbus"],
)


@pytest.fixture
async def udp_server(free_udp_port: int) -> AsyncIterator[tuple[str, int]]:
    """A Modbus UDP server with one known holding register."""
    values = [0] * 10
    values[0] = 5579  # protocol holding addr 0 -> register 0
    host, port = "127.0.0.1", free_udp_port
    async with serve_udp(holding_store(values), host, port):
        yield host, port


@backends
async def test_udp_reads(
    client_cls: type[BaseModbusConnection], udp_server: tuple[str, int]
) -> None:
    # The client is lazy: no I/O until the first request.
    host, port = udp_server
    client = client_cls(ModbusUdpParams(host=host, port=port))
    try:
        assert client.connected is False
        assert await client.for_unit(UNIT_ID).read_holding_registers(0, 1) == [5579]
        assert client.connected is True
    finally:
        await client.close()
    assert client.connected is False


@backends
async def test_udp_write_roundtrip(
    client_cls: type[BaseModbusConnection], udp_server: tuple[str, int]
) -> None:
    host, port = udp_server
    client = client_cls(ModbusUdpParams(host=host, port=port))
    try:
        unit = client.for_unit(UNIT_ID)
        await unit.write_register(0, 4242)
        assert await unit.read_holding_registers(0, 1) == [4242]
    finally:
        await client.close()


@backend_modules
async def test_connect_udp_factory_reads(
    backend: ModuleType, udp_server: tuple[str, int]
) -> None:
    # The eager factory binds the endpoint up front and returns a live handle.
    host, port = udp_server
    conn = await backend.connect_udp(host, port=port)
    try:
        assert conn.connected is True
        assert await conn.for_unit(UNIT_ID).read_holding_registers(0, 1) == [5579]
    finally:
        await conn.close()


@pytest.mark.parametrize("framer", ["rtu", "ascii"])
async def test_tmodbus_udp_non_socket_framing_rejected(framer: str) -> None:
    """tmodbus' UDP transport is MBAP-only: the other framings raise."""
    with pytest.raises(ValueError, match="pymodbus.ModbusConnection"):
        await tmodbus_backend.connect_udp("127.0.0.1", port=502, framer=framer)  # type: ignore[arg-type]
