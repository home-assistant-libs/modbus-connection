"""connect()/close() concurrency: one shared in-flight connect attempt.

The machinery under test lives entirely in the base class, so a minimal
subclass whose connect is held at a deterministic point stands in for the
backends (their own suites cover the real ``_connect_client``s).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from modbus_connection import (
    ClientClosedError,
    ModbusConnectionError,
    ModbusTcpParams,
    ModbusUnit,
)
from modbus_connection._client import BaseModbusConnection


class _GatedConnection(BaseModbusConnection):
    """Holds every connect attempt until the test releases it."""

    def __init__(self, *, connect_error: Exception | None = None) -> None:
        super().__init__(ModbusTcpParams(host="127.0.0.1"))
        self.connect_calls = 0
        self.close_calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self._connect_error = connect_error

    async def _connect_client(self) -> Any:
        self.connect_calls += 1
        self.started.set()
        await self.release.wait()
        if self._connect_error is not None:
            raise ModbusConnectionError(str(self._connect_error))
        return object()

    async def _close_client(self, client: Any) -> None:
        self.close_calls += 1

    def for_unit(self, unit_id: int) -> ModbusUnit:
        raise NotImplementedError


async def test_concurrent_connects_share_one_attempt() -> None:
    conn = _GatedConnection()
    waiters = [asyncio.create_task(conn.connect()) for _ in range(3)]
    await conn.started.wait()
    conn.release.set()
    await asyncio.gather(*waiters)

    assert conn.connect_calls == 1
    assert conn.connected is True


async def test_concurrent_connects_share_one_failure() -> None:
    conn = _GatedConnection(connect_error=OSError("refused"))
    waiters = [asyncio.create_task(conn.connect()) for _ in range(3)]
    await conn.started.wait()
    conn.release.set()
    for waiter in waiters:
        with pytest.raises(ModbusConnectionError):
            await waiter

    assert conn.connect_calls == 1  # one shared attempt; the failure is not retried
    assert conn.connected is False
    # The failed flight was cleared, so a later connect() starts a fresh attempt.
    assert conn._connect_task is None


async def test_cancelled_connect_waiter_does_not_cancel_shared_connect() -> None:
    conn = _GatedConnection()
    cancelled = asyncio.create_task(conn.connect())
    surviving = asyncio.create_task(conn.connect())
    await conn.started.wait()

    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert surviving.done() is False

    conn.release.set()
    await surviving

    assert conn.connected is True
    assert conn.connect_calls == 1


async def test_close_during_connect_closes_new_client_without_resurrection() -> None:
    conn = _GatedConnection()
    connecting = asyncio.create_task(conn.connect())
    await conn.started.wait()

    closing = asyncio.create_task(conn.close())
    await asyncio.sleep(0)
    assert closing.done() is False  # close waits out the in-flight connect
    conn.release.set()

    with pytest.raises(ClientClosedError):
        await connecting
    await closing

    assert conn.connected is False
    assert conn.close_calls == 1  # the just-connected client was disposed
    with pytest.raises(ClientClosedError):
        await conn.connect()


class _DelayedConnection(BaseModbusConnection):
    """Connects instantly; only connect_delay separates connect from ready."""

    def __init__(self, connect_delay: float) -> None:
        super().__init__(ModbusTcpParams(host="127.0.0.1"), connect_delay=connect_delay)

    async def _connect_client(self) -> Any:
        return object()

    async def _close_client(self, client: Any) -> None:
        pass

    def for_unit(self, unit_id: int) -> ModbusUnit:
        raise NotImplementedError


async def test_connect_delay_holds_the_shared_flight() -> None:
    """The delay runs inside the flight: no caller sees the client early."""
    conn = _DelayedConnection(connect_delay=0.05)
    loop = asyncio.get_running_loop()
    start = loop.time()
    waiters = [asyncio.create_task(conn.connect()) for _ in range(3)]
    await asyncio.sleep(0)
    assert conn.connected is False  # the pause holds publication of the client
    await asyncio.gather(*waiters)

    assert conn.connected is True
    assert loop.time() - start >= 0.05  # one delay, shared by all callers


async def test_connect_delay_defaults_to_none() -> None:
    conn = _DelayedConnection(connect_delay=0.0)
    await conn.connect()
    assert conn.connected is True


async def test_disconnect_waits_out_and_drops_an_in_flight_connect() -> None:
    conn = _GatedConnection()
    connecting = asyncio.create_task(conn.connect())
    await conn.started.wait()

    disconnecting = asyncio.create_task(conn.disconnect())
    await asyncio.sleep(0)
    assert disconnecting.done() is False  # waits the shared attempt out
    conn.release.set()

    await connecting  # the flight itself succeeded for its callers
    await disconnecting

    assert conn.connected is False  # ...but the client was then dropped
    assert conn.close_calls == 1
    # Not closed: connecting again establishes a fresh link.
    conn.release.set()
    await conn.connect()
    assert conn.connected is True
    assert conn.connect_calls == 2
