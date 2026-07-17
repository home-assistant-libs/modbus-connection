"""Tests for inter-request spacing (``message_spacing`` + per-unit gaps).

Spacing is enforced by the shared ``Pacer`` — both backends use it, neither
relies on a native knob — so the deterministic tests drive a fake clock against
the pacer directly, and the end-to-end tests prove both backends pace real
requests over one server.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import pytest

from modbus_connection import ModbusTcpParams, _pacing
from modbus_connection._pacing import Pacer
from modbus_connection.pymodbus import PymodbusConnection
from modbus_connection.pymodbus import connect_tcp as pymodbus_connect_tcp
from modbus_connection.tmodbus import connect_tcp as tmodbus_connect_tcp

from .conftest import UNIT_ID


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
