"""connect()/close() concurrency: one shared in-flight connect attempt.

Concurrent callers of ``connect()`` share a single backend connect and its
result; cancelling one caller leaves that attempt running for the others; and
``close()`` waits out an in-flight attempt — disposing any client it produces —
before returning. The fakes hold the connect at a deterministic point so the
races are reproducible without sockets.
"""

from __future__ import annotations

import asyncio

import pytest

import modbus_connection.pymodbus as pymodbus_backend
import modbus_connection.tmodbus as tmodbus_backend
from modbus_connection import (
    ClientClosedError,
    ModbusConnectionError,
    ModbusTcpParams,
)
from modbus_connection._client import BaseModbusConnection

BACKENDS = ["tmodbus", "pymodbus"]


class _ControlledConnect:
    """A connect attempt held at a deterministic point until a test releases it."""

    def __init__(self, *, connect_error: Exception | None = None) -> None:
        self.connected = False
        self.connect_calls = 0
        self.close_calls = 0
        self.connect_started = asyncio.Event()
        self.connect_release = asyncio.Event()
        self._connect_error = connect_error

    async def connect(self) -> bool:
        self.connect_calls += 1
        self.connect_started.set()
        await self.connect_release.wait()
        if self._connect_error is not None:
            raise self._connect_error
        self.connected = True
        return True


class _ControlledTmodbusConnect(_ControlledConnect):
    async def disconnect(self) -> None:
        self.close_calls += 1
        self.connected = False

    def for_unit_id(self, unit_id: int) -> _ControlledTmodbusConnect:
        return self


class _ControlledPymodbusConnect(_ControlledConnect):
    def close(self) -> None:
        self.close_calls += 1
        self.connected = False


def _controlled_connection(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    *,
    connect_error: Exception | None = None,
) -> tuple[BaseModbusConnection, _ControlledConnect]:
    fake: _ControlledConnect
    if backend == "tmodbus":
        fake = _ControlledTmodbusConnect(connect_error=connect_error)
        monkeypatch.setattr(
            tmodbus_backend, "create_async_tcp_client", lambda *a, **k: fake
        )
        return (
            tmodbus_backend.ModbusConnection(ModbusTcpParams(host="127.0.0.1")),
            fake,
        )
    fake = _ControlledPymodbusConnect(connect_error=connect_error)
    monkeypatch.setattr(pymodbus_backend, "AsyncModbusTcpClient", lambda *a, **k: fake)
    return (
        pymodbus_backend.ModbusConnection(ModbusTcpParams(host="127.0.0.1")),
        fake,
    )


@pytest.mark.parametrize("backend", BACKENDS)
async def test_concurrent_connects_share_one_attempt(
    monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    client, fake = _controlled_connection(monkeypatch, backend)
    waiters = [asyncio.create_task(client.connect()) for _ in range(3)]
    await fake.connect_started.wait()
    fake.connect_release.set()
    await asyncio.gather(*waiters)

    assert fake.connect_calls == 1  # one in-flight attempt shared by all three
    assert client.connected is True
    await client.close()


@pytest.mark.parametrize("backend", BACKENDS)
async def test_concurrent_connects_share_one_failure(
    monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    client, fake = _controlled_connection(
        monkeypatch, backend, connect_error=OSError("refused")
    )

    async def connect_and_capture() -> None:
        with pytest.raises(ModbusConnectionError):
            await client.connect()

    waiters = [asyncio.create_task(connect_and_capture()) for _ in range(3)]
    await fake.connect_started.wait()
    fake.connect_release.set()
    await asyncio.gather(*waiters)

    assert fake.connect_calls == 1  # one shared attempt; the failure is not retried
    assert client.connected is False
    # The failed flight was cleared, so a later connect() starts a fresh attempt.
    assert client._connect_task is None


@pytest.mark.parametrize("backend", BACKENDS)
async def test_cancelled_connect_waiter_does_not_cancel_shared_connect(
    monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    client, fake = _controlled_connection(monkeypatch, backend)
    cancelled_waiter = asyncio.create_task(client.connect())
    surviving_waiter = asyncio.create_task(client.connect())
    await fake.connect_started.wait()

    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter
    assert surviving_waiter.done() is False

    fake.connect_release.set()
    await surviving_waiter

    assert client.connected is True
    assert fake.connect_calls == 1
    await client.close()
    assert fake.close_calls == 1


@pytest.mark.parametrize("backend", BACKENDS)
async def test_close_during_connect_closes_new_client_without_resurrection(
    monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    client, fake = _controlled_connection(monkeypatch, backend)
    connecting = asyncio.create_task(client.connect())
    await fake.connect_started.wait()

    closing = asyncio.create_task(client.close())
    await asyncio.sleep(0)
    assert closing.done() is False  # close waits out the in-flight connect
    fake.connect_release.set()

    with pytest.raises(ClientClosedError):
        await connecting
    await closing

    assert client.connected is False
    assert fake.connect_calls == 1
    assert fake.close_calls == 1  # the just-connected client was disposed
    with pytest.raises(ClientClosedError):
        await client.connect()
