"""ModbusTcpParams(framer="rtu") talks RTU-over-TCP (transparent gateways)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

import modbus_connection.pymodbus as pymodbus_backend
import modbus_connection.tmodbus as tmodbus_backend
from modbus_connection import ModbusTcpParams

from .modbus_server import holding_store, serve_rtu_over_tcp, serve_stream

UNIT_ID = 246


@pytest.fixture
async def rtu_server(free_port: int) -> AsyncIterator[tuple[str, int]]:
    """A server that frames RTU-over-TCP, like a serial-to-Ethernet gateway."""
    values = [0] * 10
    values[0] = 5579  # protocol holding addr 0 -> register 0
    host, port = "127.0.0.1", free_port
    async with serve_rtu_over_tcp(holding_store(values), host, port):
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
        assert await conn.for_unit(UNIT_ID).diagnostics(0x0000, 0x1234) == 0x1234
    finally:
        await conn.close()


# -- vendor-specific diagnostics sub-function ---------------------------------

# tmodbus has no PDU for a sub-function outside the spec, so it cannot decode
# one server-side. This device answers from raw bytes instead.
_VENDOR_SUB_FUNCTION = 0x0064


def _crc(frame: bytes) -> bytes:
    """The Modbus RTU CRC-16 trailing every frame."""
    register = 0xFFFF
    for byte in frame:
        register ^= byte
        for _ in range(8):
            register = (register >> 1) ^ 0xA001 if register & 1 else register >> 1
    return register.to_bytes(2, "little")


async def _vendor_device(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Answer one diagnostics request with 0xBEEF, then hang up.

    ``serve_stream`` aborts the connections whose handler is still running, so
    a handler that returns has to close its own: an open connection nothing is
    tracking blocks ``wait_closed()`` on Python 3.12 until the job times out.
    """
    try:
        await reader.readexactly(8)  # unit + function + sub-function + data + CRC
        body = (
            bytes([UNIT_ID, 0x08])
            + _VENDOR_SUB_FUNCTION.to_bytes(2, "big")
            + (0xBEEF).to_bytes(2, "big")
        )
        writer.write(body + _crc(body))
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def test_tmodbus_rtu_over_tcp_vendor_sub_function(free_port: int) -> None:
    """An unregistered sub-function frames off our own PDU, not tmodbus's."""
    host, port = "127.0.0.1", free_port
    async with serve_stream(_vendor_device, host, port):
        conn = tmodbus_backend.ModbusConnection(
            ModbusTcpParams(host=host, port=port, framer="rtu"), timeout=3
        )
        try:
            unit = conn.for_unit(UNIT_ID)
            assert await unit.diagnostics(_VENDOR_SUB_FUNCTION, 0x1234) == 0xBEEF
        finally:
            await conn.close()
