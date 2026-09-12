"""Tests for request serialization and spacing (``message_spacing`` + per-unit gaps).

Both are enforced by the shared ``Pacer`` — both backends use it, neither
relies on a native knob — so the deterministic tests drive a fake clock against
the pacer directly, and the end-to-end tests prove both backends pace real
requests over one server.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import pytest

from modbus_connection import (
    ModbusSerialParams,
    ModbusTcpParams,
    ModbusUnit,
    _client,
    _pacing,
)
from modbus_connection._client import BaseModbusConnection
from modbus_connection._pacing import Pacer
from modbus_connection.pymodbus import PymodbusConnection
from modbus_connection.pymodbus import connect_tcp as pymodbus_connect_tcp
from modbus_connection.tmodbus import connect_tcp as tmodbus_connect_tcp

from .conftest import UNIT_ID

SERIAL_DEFAULT_SPACING = 0.03


def _fake_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Callable[[float], None], list[float]]:
    """Drive the pacer's clock + sleep off a manual timeline.

    Returns ``(advance, sleeps)``: call ``advance`` to model time a request
    spends on the wire; ``sleeps`` records every ``asyncio.sleep`` the pacer
    performs (each also advances the clock).
    """
    now = 1000.0  # a realistic (large) value so the first request is free
    sleeps: list[float] = []

    def advance(delta: float) -> None:
        nonlocal now
        now += delta

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        advance(delay)

    monkeypatch.setattr(_pacing.time, "monotonic", lambda: now)
    monkeypatch.setattr(_pacing.asyncio, "sleep", fake_sleep)
    return advance, sleeps


# -- validation ---------------------------------------------------------------


def test_pacer_rejects_negative_message_spacing() -> None:
    with pytest.raises(ValueError):
        Pacer(message_spacing=-0.1)


def test_pacer_rejects_negative_unit_spacing() -> None:
    with pytest.raises(ValueError):
        Pacer().set_unit_spacing(UNIT_ID, -0.1)


def test_connection_rejects_negative_message_spacing() -> None:
    with pytest.raises(ValueError):
        PymodbusConnection(ModbusTcpParams(host="test"), message_spacing=-0.1)


async def test_tmodbus_connect_rejects_negative_message_spacing() -> None:
    with pytest.raises(ValueError):
        await tmodbus_connect_tcp("127.0.0.1", port=502, message_spacing=-0.1)


# -- the transport default ----------------------------------------------------


def test_a_serial_link_paces_itself() -> None:
    """An RS485 adapter needs time to switch direction, whoever opens the link."""
    conn = PymodbusConnection(ModbusSerialParams(device="/dev/ttyUSB0"))

    assert conn._pacer._message_spacing == SERIAL_DEFAULT_SPACING


@pytest.mark.parametrize("spacing", [0, 0.1], ids=["disabled", "widened"])
def test_an_explicit_gap_overrides_the_serial_default(spacing: float) -> None:
    conn = PymodbusConnection(
        ModbusSerialParams(device="/dev/ttyUSB0"), message_spacing=spacing
    )

    assert conn._pacer._message_spacing == spacing


@pytest.mark.filterwarnings("ignore:ModbusTcpParams:DeprecationWarning")
def test_a_serial_framing_over_tcp_paces_itself_too() -> None:
    """One serial line, so both spellings of it get the same gap."""
    conn = PymodbusConnection(ModbusTcpParams(host="test", port=8899, framer="rtu"))

    assert conn._pacer._message_spacing == SERIAL_DEFAULT_SPACING


def test_a_socket_link_paces_itself_not_at_all() -> None:
    conn = PymodbusConnection(ModbusTcpParams(host="test"))

    assert conn._pacer._message_spacing == 0


# -- the connection-wide gap --------------------------------------------------


async def test_noop_when_disabled() -> None:
    pacer = Pacer(0.0)
    start = time.monotonic()
    for _ in range(5):
        async with pacer.paced(UNIT_ID):
            pass
    assert time.monotonic() - start < 0.05  # never slept


async def test_waits_the_gap_after_each_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    advance, sleeps = _fake_clock(monkeypatch)
    pacer = Pacer(0.25)
    async with pacer.paced(UNIT_ID):  # first request: runs immediately
        advance(0.10)  # ... and occupies the wire for 100 ms
    async with pacer.paced(UNIT_ID):  # nothing idle since -> wait the full gap
        pass
    assert sleeps == [pytest.approx(0.25)]


async def test_no_wait_when_already_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    advance, sleeps = _fake_clock(monkeypatch)
    pacer = Pacer(0.25)
    async with pacer.paced(UNIT_ID):
        pass
    advance(0.50)  # caller idled longer than the spacing on its own
    async with pacer.paced(UNIT_ID):
        pass
    assert sleeps == []


async def test_serializes_concurrent_callers() -> None:
    """Concurrent callers (the shared-connection case) still line up in order."""
    pacer = Pacer(0.02)

    async def one() -> None:
        async with pacer.paced(UNIT_ID):
            pass

    start = time.monotonic()
    await asyncio.gather(*(one() for _ in range(5)))
    # Five requests means four gaps of at least `spacing` each.
    assert time.monotonic() - start >= 0.02 * 4


# -- serialization ------------------------------------------------------------


async def test_serializes_concurrent_callers_without_spacing() -> None:
    """One link carries one request at a time, spacing configured or not."""
    pacer = Pacer(0.0)
    in_flight = 0
    peak = 0

    async def one(unit_id: int) -> None:
        nonlocal in_flight, peak
        async with pacer.paced(unit_id):
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0)
            in_flight -= 1

    await asyncio.gather(*(one(unit_id) for unit_id in range(5)))
    assert peak == 1


class _TrackingConnection(BaseModbusConnection):
    """A connection whose client is a stand-in, counting its teardowns."""

    def __init__(self) -> None:
        super().__init__(ModbusTcpParams(host="127.0.0.1"))
        self.closed_clients = 0

    async def _connect_client(self) -> Any:
        return object()

    async def _close_client(self, client: Any) -> None:
        self.closed_clients += 1

    def for_unit(self, unit_id: int) -> ModbusUnit:
        raise NotImplementedError


@pytest.mark.parametrize("teardown", ["disconnect", "close"])
async def test_teardown_waits_for_the_request_in_flight(teardown: str) -> None:
    """A link is never torn down under a request."""
    conn = _TrackingConnection()
    await conn.connect()

    async with conn._pacer.paced(UNIT_ID):
        task = asyncio.create_task(getattr(conn, teardown)())
        for _ in range(3):  # let the teardown run up to the lock
            await asyncio.sleep(0)
        assert conn.closed_clients == 0
        assert conn.connected is True

    await task
    assert conn.closed_clients == 1
    assert conn.connected is False


@pytest.mark.parametrize("teardown", ["disconnect", "close"])
async def test_teardown_gives_up_on_a_wedged_request(
    monkeypatch: pytest.MonkeyPatch, teardown: str
) -> None:
    """A request that does not answer must not hold the link up."""
    monkeypatch.setattr(_client, "_TEARDOWN_GRACE", 0.01)
    conn = _TrackingConnection()
    await conn.connect()

    async with conn._pacer.paced(UNIT_ID):  # never answers
        await getattr(conn, teardown)()
        assert conn.closed_clients == 1
        assert conn.connected is False


# -- per-unit gap on top of the connection-wide gap ---------------------------


async def test_per_unit_spacing_paces_only_that_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    advance, sleeps = _fake_clock(monkeypatch)
    pacer = Pacer(0.0)
    pacer.set_unit_spacing(5, 0.25)

    async with pacer.paced(5):  # first request to unit 5: free
        advance(0.10)
    async with pacer.paced(5):  # back-to-back on unit 5 -> waits the unit gap
        pass
    async with pacer.paced(6):  # a different unit shares the link, not the gap
        pass
    assert sleeps == [pytest.approx(0.25)]  # only unit 5 ever waited


async def test_per_unit_and_link_spacing_take_the_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sleeps = _fake_clock(monkeypatch)
    pacer = Pacer(0.05)
    pacer.set_unit_spacing(5, 0.25)

    async with pacer.paced(5):  # first request: free
        pass
    async with pacer.paced(5):  # waits max(link 0.05, unit 0.25)
        pass
    assert sleeps == [pytest.approx(0.25)]


async def test_clearing_unit_spacing_stops_pacing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    advance, sleeps = _fake_clock(monkeypatch)
    pacer = Pacer(0.0)
    pacer.set_unit_spacing(5, 0.25)
    async with pacer.paced(5):
        advance(0.10)
    pacer.set_unit_spacing(5, 0)  # cleared -> no more waiting
    async with pacer.paced(5):
        pass
    assert sleeps == []


# -- end to end: both backends actually pace ----------------------------------


@pytest.mark.parametrize("backend", ["pymodbus", "tmodbus"])
async def test_backend_paces_requests(
    modbus_server: tuple[str, int], backend: str
) -> None:
    host, port = modbus_server
    spacing = 0.05
    if backend == "pymodbus":
        conn = await pymodbus_connect_tcp(host, port=port, message_spacing=spacing)
    else:
        conn = await tmodbus_connect_tcp(host, port=port, message_spacing=spacing)
    try:
        unit = conn.for_unit(UNIT_ID)
        start = time.monotonic()
        for _ in range(4):
            await unit.read_holding_registers(0, 1)
        elapsed = time.monotonic() - start
    finally:
        await conn.close()
    # Four requests means three gaps of at least `spacing` each.
    assert elapsed >= spacing * 3


@pytest.mark.parametrize("backend", ["pymodbus", "tmodbus"])
async def test_backend_paces_a_single_unit(
    modbus_server: tuple[str, int], backend: str
) -> None:
    host, port = modbus_server
    spacing = 0.05
    if backend == "pymodbus":
        conn = await pymodbus_connect_tcp(host, port=port)
    else:
        conn = await tmodbus_connect_tcp(host, port=port)
    try:
        conn.for_unit(UNIT_ID).set_message_spacing(spacing)
        # The gap is keyed by unit id, so a second handle is paced too.
        poller = conn.for_unit(UNIT_ID)
        start = time.monotonic()
        for _ in range(4):
            await poller.read_holding_registers(0, 1)
        elapsed = time.monotonic() - start
    finally:
        await conn.close()
    # Four requests means three gaps of at least `spacing` each.
    assert elapsed >= spacing * 3
