"""ModbusTcpParams(framer="ascii") tunnels Modbus ASCII frames over TCP.

The rest of the suite serves these tests from tmodbus, but tmodbus frames ASCII
only over a serial line — it has no ASCII-over-TCP server — so this module keeps
pymodbus's. Only the pymodbus client can speak this combination anyway.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from pymodbus import FramerType
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

from modbus_connection import ModbusTcpParams
from modbus_connection.pymodbus import ModbusConnection
from modbus_connection.tmodbus import connect_tcp as tmodbus_connect_tcp

UNIT_ID = 1


@pytest.fixture
async def ascii_tcp_server(free_port: int) -> AsyncIterator[tuple[str, int]]:
    """A TCP server that frames Modbus ASCII over the stream."""
    values = [0] * 10
    values[0] = 5579  # protocol holding addr 0 -> register 0
    empty_bits = [SimData(0, values=[False] * 10, datatype=DataType.BITS)]
    holding = [SimData(0, values=values, datatype=DataType.REGISTERS)]
    empty_regs = [SimData(0, values=[0] * 10, datatype=DataType.REGISTERS)]
    # id=0 serves every unit id.
    context = SimDevice(0, simdata=(empty_bits, empty_bits, holding, empty_regs))
    host, port = "127.0.0.1", free_port
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
