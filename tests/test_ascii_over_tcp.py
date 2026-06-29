"""connect_tcp(framer="ascii") tunnels Modbus ASCII frames over TCP.

pymodbus speaks this; tmodbus has no ASCII-over-TCP transport and raises.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator

import pytest
from pymodbus import FramerType
from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server import ModbusTcpServer

from modbus_connection.pymodbus import connect_tcp as pymodbus_connect_tcp
from modbus_connection.tmodbus import connect_tcp as tmodbus_connect_tcp

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
    # pymodbus 3.13: block address is 1-based and FC03 (holding) is served from
    # the `ir` slot; the device must be passed directly (not a {id: device} dict).
    device = ModbusDeviceContext(ir=ModbusSequentialDataBlock(1, values))
    context = ModbusServerContext(devices=device)
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
    conn = await pymodbus_connect_tcp(host, port=port, framer="ascii")
    try:
        assert await conn.for_unit(UNIT_ID).read_holding_registers(0, 1) == [5579]
    finally:
        await conn.close()


async def test_tmodbus_ascii_over_tcp_not_implemented() -> None:
    """tmodbus has no ASCII-over-TCP transport: framer="ascii" raises."""
    with pytest.raises(NotImplementedError):
        await tmodbus_connect_tcp("127.0.0.1", framer="ascii")
