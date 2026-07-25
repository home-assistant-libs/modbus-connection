"""ModbusTcpParams(framer="rtu") talks RTU-over-TCP (transparent gateways)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from pymodbus import FramerType
from pymodbus.server import ModbusTcpServer

import modbus_connection.pymodbus as pymodbus_backend
import modbus_connection.tmodbus as tmodbus_backend
from modbus_connection import ModbusTcpParams

from .conftest import sim_holding_device

UNIT_ID = 246


@pytest.fixture
async def rtu_server(free_port: int) -> AsyncIterator[tuple[str, int]]:
    """A server that frames RTU-over-TCP, like a serial-to-Ethernet gateway."""
    values = [0] * 10
    values[0] = 5579  # protocol holding addr 0 -> register 0
    context = sim_holding_device(values)
    host, port = "127.0.0.1", free_port
    server = ModbusTcpServer(context, framer=FramerType.RTU, address=(host, port))
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


async def test_pymodbus_rtu_over_tcp_reads(rtu_server: tuple[str, int]) -> None:
    host, port = rtu_server
    conn = pymodbus_backend.ModbusConnection(
        ModbusTcpParams(host=host, port=port, framer="rtu")
    )
    try:
        assert await conn.for_unit(UNIT_ID).read_holding_registers(0, 1) == [5579]
    finally:
        await conn.close()


async def test_tmodbus_rtu_over_tcp_reads(rtu_server: tuple[str, int]) -> None:
    host, port = rtu_server
    conn = tmodbus_backend.ModbusConnection(
        ModbusTcpParams(host=host, port=port, framer="rtu")
    )
    try:
        assert await conn.for_unit(UNIT_ID).read_holding_registers(0, 1) == [5579]
    finally:
        await conn.close()
