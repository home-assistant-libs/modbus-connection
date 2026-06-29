"""connect_serial over a pty bridge: RTU and ASCII framing, both backends.

A real serial link is emulated with two pty pairs whose masters are wired
together by an asyncio relay, so a pymodbus serial *server* on one slave and the
library's serial *client* on the other exchange real framed bytes — exercising
the ASCII framer added alongside RTU.
"""

from __future__ import annotations

import asyncio
import os
import pty
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from pymodbus import FramerType
from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server import ModbusSerialServer

from modbus_connection.pymodbus import connect_serial as pymodbus_connect_serial
from modbus_connection.tmodbus import connect_serial as tmodbus_connect_serial

UNIT_ID = 1


def _make_bridge(loop: asyncio.AbstractEventLoop) -> tuple[str, str, list[int]]:
    """Create two pty pairs and relay bytes between their masters.

    Returns (server_port, client_port, master_fds). The server opens
    ``server_port``; everything written there is copied to ``client_port`` and
    vice versa.
    """
    master_a, slave_a = pty.openpty()
    master_b, slave_b = pty.openpty()
    for fd in (master_a, master_b):
        os.set_blocking(fd, False)

    def relay(src: int, dst: int) -> None:
        try:
            data = os.read(src, 4096)
        except (BlockingIOError, OSError):
            return
        if data:
            try:
                os.write(dst, data)
            except OSError:
                pass

    loop.add_reader(master_a, relay, master_a, master_b)
    loop.add_reader(master_b, relay, master_b, master_a)
    return os.ttyname(slave_a), os.ttyname(slave_b), [master_a, master_b]


@pytest.fixture
async def serial_port() -> AsyncIterator[Callable[[FramerType], Any]]:
    """Async factory that starts a serial server and tears it down after the test."""
    cleanups: list[Any] = []

    async def start(framer: FramerType) -> str:
        loop = asyncio.get_running_loop()
        server_port, client_port, masters = _make_bridge(loop)
        values = [0] * 10
        values[0] = 5579
        device = ModbusDeviceContext(ir=ModbusSequentialDataBlock(1, values))
        context = ModbusServerContext(devices=device)
        server = ModbusSerialServer(
            context, framer=framer, port=server_port, baudrate=9600
        )
        task = asyncio.create_task(server.serve_forever())
        await asyncio.sleep(0.3)

        async def cleanup() -> None:
            await server.shutdown()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            for fd in masters:
                loop.remove_reader(fd)
                os.close(fd)

        cleanups.append(cleanup)
        return client_port

    yield start

    for cleanup in cleanups:
        await cleanup()


@pytest.mark.parametrize(
    ("framing", "framer"),
    [("rtu", FramerType.RTU), ("ascii", FramerType.ASCII)],
)
async def test_pymodbus_serial_reads(
    serial_port: Callable[[FramerType], Any], framing: str, framer: FramerType
) -> None:
    client_port = await serial_port(framer)
    conn = await pymodbus_connect_serial(
        client_port, framer=framing, baudrate=9600, timeout=2
    )
    try:
        assert await conn.for_unit(UNIT_ID).read_holding_registers(0, 1) == [5579]
    finally:
        await conn.close()


@pytest.mark.parametrize(
    ("framing", "framer"),
    [("rtu", FramerType.RTU), ("ascii", FramerType.ASCII)],
)
async def test_tmodbus_serial_reads(
    serial_port: Callable[[FramerType], Any], framing: str, framer: FramerType
) -> None:
    client_port = await serial_port(framer)
    conn = await tmodbus_connect_serial(
        client_port, framer=framing, unit_id=UNIT_ID, baudrate=9600
    )
    try:
        assert await conn.for_unit(UNIT_ID).read_holding_registers(0, 1) == [5579]
    finally:
        await conn.close()
