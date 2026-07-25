"""Real-socket proof that a lost link is detected and the next request recovers.

Nothing is replayed: the request that meets the drop raises, the transport's
connection-lost hook clears the dead client, and the *next* request reconnects.
These tests are the regression net for that contract, so they use real sockets
(a Modbus server that goes away, and a server that hangs up mid-request) rather
than fakes.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable

import pytest
from pymodbus.server import ModbusTcpServer

from modbus_connection import (
    ModbusConnection,
    ModbusError,
    ModbusTcpParams,
    ModbusTimeoutError,
)
from modbus_connection.pymodbus import ModbusConnection as PymodbusConnection
from modbus_connection.tmodbus import ModbusConnection as TmodbusConnection

from .conftest import UNIT_ID, sim_holding_device

BACKENDS: dict[str, Callable[[ModbusTcpParams], ModbusConnection]] = {
    "pymodbus": lambda params: PymodbusConnection(params, timeout=1),
    "tmodbus": lambda params: TmodbusConnection(params, timeout=1),
}

VALUE = 4321


async def _wait_until_listening(host: str, port: int) -> None:
    for _ in range(100):
        try:
            reader, writer = await asyncio.open_connection(host, port)
        except OSError:
            await asyncio.sleep(0.02)
            continue
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return
    pytest.fail(f"server on {host}:{port} never started listening")


class _Server:
    """A Modbus TCP server on a fixed port that tests start and stop at will."""

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._server: ModbusTcpServer | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._server = ModbusTcpServer(
            sim_holding_device([VALUE]), address=(self._host, self._port)
        )
        self._task = asyncio.create_task(self._server.serve_forever())
        await _wait_until_listening(self._host, self._port)

    async def stop(self) -> None:
        assert self._server is not None
        assert self._task is not None
        await self._server.shutdown()
        self._task.cancel()
        with contextlib.suppress(BaseException):
            await self._task
        self._server = self._task = None
        # Give the client's event loop a turn to process the closed socket.
        await asyncio.sleep(0.05)


@pytest.fixture
async def server_port(free_port: int) -> AsyncIterator[tuple[str, int]]:
    yield "127.0.0.1", free_port


@pytest.fixture
async def server(server_port: tuple[str, int]) -> AsyncIterator[_Server]:
    host, port = server_port
    running = _Server(host, port)
    await running.start()
    try:
        yield running
    finally:
        if running._server is not None:
            await running.stop()


@pytest.fixture(params=list(BACKENDS))
async def connection(
    request: pytest.FixtureRequest, server_port: tuple[str, int]
) -> AsyncIterator[ModbusConnection]:
    host, port = server_port
    conn = BACKENDS[request.param](ModbusTcpParams(host=host, port=port))
    try:
        yield conn
    finally:
        await conn.close()


async def test_server_gone_between_requests_downs_the_link(
    server: _Server, connection: ModbusConnection
) -> None:
    """The loss hook clears the client the moment the server goes away."""
    unit = connection.for_unit(UNIT_ID)
    assert await unit.read_holding_registers(0, 1) == [VALUE]

    await server.stop()

    # No request needed: the transport reported the loss on its own.
    assert connection.connected is False


async def test_next_request_reconnects_after_the_server_returns(
    server: _Server, connection: ModbusConnection
) -> None:
    """A device that disappears and comes back is polled again, same handles."""
    unit = connection.for_unit(UNIT_ID)
    assert await unit.read_holding_registers(0, 1) == [VALUE]

    await server.stop()
    with pytest.raises(ModbusError):
        await unit.read_holding_registers(0, 1)  # nothing is listening
    assert connection.connected is False

    await server.start()

    assert await unit.read_holding_registers(0, 1) == [VALUE]
    assert connection.connected is True


async def test_mid_request_drop_raises_and_downs_the_link(
    server_port: tuple[str, int], connection: ModbusConnection
) -> None:
    """A connection killed with a request in flight raises and clears the client.

    The server accepts, reads the request, and aborts the socket without ever
    answering — the drop lands squarely between dispatch and response.
    """
    host, port = server_port

    async def hangup(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await reader.read(1)  # the request is now in flight
        writer.transport.abort()

    killer = await asyncio.start_server(hangup, host, port)
    unit = connection.for_unit(UNIT_ID)
    try:
        with pytest.raises(ModbusError):
            await unit.read_holding_registers(0, 1)
        assert connection.connected is False
    finally:
        killer.close()
        await killer.wait_closed()

    # The same connection and handle recover once the device is back.
    live = _Server(host, port)
    await live.start()
    try:
        assert await unit.read_holding_registers(0, 1) == [VALUE]
        assert connection.connected is True
    finally:
        await live.stop()


async def test_open_but_silent_socket_times_out_and_keeps_the_link(
    server_port: tuple[str, int], connection: ModbusConnection
) -> None:
    """A device that stops answering without dropping the socket is a timeout.

    Nothing reports a loss — the socket is still open — so the link stays up and
    the request surfaces as a timeout, exactly as it does for a device that is
    merely slow. This is the half-open case (a peer that vanished without a
    FIN/RST looks the same from here): recovery waits for the OS to tear the
    socket down.
    """
    host, port = server_port
    accepted: list[asyncio.StreamWriter] = []

    async def silent(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        accepted.append(writer)
        await reader.read()  # never answers; returns once the socket goes away

    mute = await asyncio.start_server(silent, host, port)
    unit = connection.for_unit(UNIT_ID)
    try:
        with pytest.raises(ModbusTimeoutError):
            await unit.read_holding_registers(0, 1)
        assert connection.connected is True
    finally:
        for writer in accepted:
            writer.transport.abort()
        mute.close()
        await mute.wait_closed()
