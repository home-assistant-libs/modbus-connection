"""Serial ModbusConnection over a pty bridge: RTU and ASCII framing, both backends.

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
from contextlib import AsyncExitStack
from typing import Any, Literal

import pytest

import modbus_connection.pymodbus as pymodbus_backend
import modbus_connection.tmodbus as tmodbus_backend
from modbus_connection import ModbusSerialParams

from .modbus_server import holding_store, serve_serial

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
async def serial_port() -> AsyncIterator[Callable[[str], Any]]:
    """Async factory that starts a serial server and tears it down after the test."""
    stack = AsyncExitStack()

    async def start(framing: str) -> str:
        loop = asyncio.get_running_loop()
        server_port, client_port, masters = _make_bridge(loop)
        values = [0] * 10
        values[0] = 5579
        await stack.enter_async_context(
            serve_serial(holding_store(values), server_port, framing)
        )

        @stack.push_async_callback
        async def close_bridge() -> None:
            for fd in masters:
                loop.remove_reader(fd)
                os.close(fd)

        return client_port

    try:
        yield start
    finally:
        await stack.aclose()


@pytest.mark.parametrize("backend", ["pymodbus", "tmodbus"])
@pytest.mark.parametrize("framing", ["rtu", "ascii"])
async def test_serial_reads(
    serial_port: Callable[[str], Any],
    backend: str,
    framing: Literal["rtu", "ascii"],
) -> None:
    client_port = await serial_port(framing)
    params = ModbusSerialParams(device=client_port, baudrate=9600, framer=framing)
    if backend == "pymodbus":
        conn = pymodbus_backend.ModbusConnection(params, timeout=2)
    else:
        conn = tmodbus_backend.ModbusConnection(params, timeout=2)
    try:
        assert await conn.for_unit(UNIT_ID).read_holding_registers(0, 1) == [5579]
        if backend == "tmodbus":
            assert conn._client.transport.base_transport.timeout == 2
    finally:
        await conn.close()
