"""The pymodbus ModbusConnection talks Modbus over UDP (tmodbus has no UDP)."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator

import pytest
from pymodbus import FramerType
from pymodbus.server import ModbusUdpServer

from modbus_connection import ModbusUdpParams
from modbus_connection.pymodbus import ModbusConnection, connect_udp

from .conftest import sim_holding_device

UNIT_ID = 1


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def udp_server() -> AsyncIterator[tuple[str, int]]:
    """A Modbus UDP server with one known holding register."""
    values = [0] * 10
    values[0] = 5579  # protocol holding addr 0 -> register 0
    context = sim_holding_device(values)
    host, port = "127.0.0.1", _free_udp_port()
    server = ModbusUdpServer(context, framer=FramerType.SOCKET, address=(host, port))
    task = asyncio.create_task(server.serve_forever())
    await asyncio.sleep(0.2)
    try:
        yield host, port
    finally:
        await server.shutdown()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def test_udp_reads(udp_server: tuple[str, int]) -> None:
    # The client is lazy: no I/O until the first request.
    host, port = udp_server
    client = ModbusConnection(ModbusUdpParams(host=host, port=port))
    try:
        assert client.connected is False
        assert await client.for_unit(UNIT_ID).read_holding_registers(0, 1) == [5579]
        assert client.connected is True
    finally:
        await client.close()
    assert client.connected is False


async def test_udp_write_roundtrip(udp_server: tuple[str, int]) -> None:
    host, port = udp_server
    client = ModbusConnection(ModbusUdpParams(host=host, port=port))
    try:
        unit = client.for_unit(UNIT_ID)
        await unit.write_register(0, 4242)
        assert await unit.read_holding_registers(0, 1) == [4242]
    finally:
        await client.close()


async def test_connect_udp_factory_reads(udp_server: tuple[str, int]) -> None:
    # The eager factory binds the endpoint up front and returns a live handle.
    host, port = udp_server
    conn = await connect_udp(host, port=port)
    try:
        assert conn.connected is True
        assert await conn.for_unit(UNIT_ID).read_holding_registers(0, 1) == [5579]
    finally:
        await conn.close()
