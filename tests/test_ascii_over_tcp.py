"""ModbusTcpParams(framer="ascii") tunnels Modbus ASCII frames over TCP.

The pymodbus ModbusConnection speaks this; tmodbus has no ASCII-over-TCP transport
(its client rejects the framer at construction — see test_framer_guard).
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator

import pytest
from pymodbus import FramerType
from pymodbus.server import ModbusTcpServer

from modbus_connection import ModbusTcpParams
from modbus_connection.pymodbus import ModbusConnection
from modbus_connection.tmodbus import connect_tcp as tmodbus_connect_tcp

from .conftest import sim_holding_device

UNIT_ID = 1


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def ascii_tcp_server() -> AsyncIterator[tuple[str, int]]:
    """A TCP server that frames Modbus ASCII over the stream."""
    values = [0] * 10
    values[0] = 5579  # protocol holding addr 0 -> register 0
    context = sim_holding_device(values)
    host, port = "127.0.0.1", _free_port()
    server = ModbusTcpServer(context, framer=FramerType.ASCII, address=(host, port))
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


async def test_pymodbus_ascii_over_tcp_reads(ascii_tcp_server: tuple[str, int]) -> None:
    host, port = ascii_tcp_server
    conn = ModbusConnection(ModbusTcpParams(host=host, port=port, framer="ascii"))
    try:
        assert await conn.for_unit(UNIT_ID).read_holding_registers(0, 1) == [5579]
    finally:
        await conn.close()


async def test_tmodbus_ascii_over_tcp_rejected() -> None:
    """tmodbus has no ASCII-over-TCP transport: framer="ascii" raises."""
    with pytest.raises(ValueError, match="pymodbus.ModbusConnection"):
        await tmodbus_connect_tcp("127.0.0.1", framer="ascii")
