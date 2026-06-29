"""connect_udp talks Modbus over UDP (pymodbus only; tmodbus has no UDP)."""

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
from pymodbus.server import ModbusUdpServer

from modbus_connection.pymodbus import connect_udp as pymodbus_connect_udp
from modbus_connection.tmodbus import connect_udp as tmodbus_connect_udp

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
    # pymodbus 3.13: block address is 1-based, FC03 (holding) is served from the
    # `ir` slot, and the device must be passed directly (not a {id: device} dict).
    device = ModbusDeviceContext(ir=ModbusSequentialDataBlock(1, values))
    context = ModbusServerContext(devices=device)
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


async def test_pymodbus_udp_reads(udp_server: tuple[str, int]) -> None:
    host, port = udp_server
    conn = await pymodbus_connect_udp(host, port=port)
    try:
        assert conn.connected is True
        assert await conn.for_unit(UNIT_ID).read_holding_registers(0, 1) == [5579]
    finally:
        await conn.close()


async def test_pymodbus_udp_write_roundtrip(udp_server: tuple[str, int]) -> None:
    host, port = udp_server
    conn = await pymodbus_connect_udp(host, port=port)
    try:
        unit = conn.for_unit(UNIT_ID)
        await unit.write_register(0, 4242)
        assert await unit.read_holding_registers(0, 1) == [4242]
    finally:
        await conn.close()


async def test_tmodbus_udp_not_implemented() -> None:
    """tmodbus ships no UDP transport: connect_udp raises NotImplementedError."""
    with pytest.raises(NotImplementedError):
        await tmodbus_connect_udp("127.0.0.1", port=502)
