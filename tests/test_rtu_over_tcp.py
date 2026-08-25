"""ModbusTcpParams(framer="rtu") talks RTU-over-TCP (transparent gateways)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

import modbus_connection.pymodbus as pymodbus_backend
import modbus_connection.tmodbus as tmodbus_backend
from modbus_connection import ModbusTcpParams

from .conftest import BUS_MESSAGE_COUNT
from .modbus_server import holding_store, serve_rtu_over_tcp

UNIT_ID = 246


@pytest.fixture
async def rtu_server(free_port: int) -> AsyncIterator[tuple[str, int]]:
    """A server that frames RTU-over-TCP, like a serial-to-Ethernet gateway."""
    values = [0] * 10
    values[0] = 5579  # protocol holding addr 0 -> register 0
    store = holding_store(values)
    store.bus_message_count = BUS_MESSAGE_COUNT
    host, port = "127.0.0.1", free_port
    async with serve_rtu_over_tcp(store, host, port):
        yield host, port


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


async def test_tmodbus_rtu_over_tcp_diagnostics(rtu_server: tuple[str, int]) -> None:
    """RTU frames by length, so the diagnostics request has to encode exactly."""
    host, port = rtu_server
    conn = tmodbus_backend.ModbusConnection(
        ModbusTcpParams(host=host, port=port, framer="rtu")
    )
    try:
        assert await conn.for_unit(UNIT_ID).diagnostics(0x000B) == BUS_MESSAGE_COUNT
    finally:
        await conn.close()
