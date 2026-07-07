"""Tests for inter-request spacing (``message_spacing``).

For pymodbus the gap is implemented in this package (pymodbus has no native
knob); the deterministic test drives a fake clock, the rest use real (small)
timing. For tmodbus the parameter is forwarded to the transport's native
``wait_between_requests``, so the test there just checks the wiring and that
the backend paces requests end to end.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import pytest

from modbus_connection import pymodbus
from modbus_connection.pymodbus import PymodbusConnection
from modbus_connection.pymodbus import connect_tcp as pymodbus_connect_tcp
from modbus_connection.tmodbus import connect_tcp as tmodbus_connect_tcp

from .conftest import UNIT_ID

# -- pymodbus: the in-package shim --------------------------------------------


def test_negative_spacing_raises() -> None:
    with pytest.raises(ValueError):
        PymodbusConnection(None, message_spacing=-0.1)  # type: ignore[arg-type]


async def test_paced_is_noop_when_disabled() -> None:
    conn = PymodbusConnection(None, message_spacing=0.0)  # type: ignore[arg-type]
    start = time.monotonic()
    for _ in range(5):
        async with conn._paced(UNIT_ID):
            pass
    assert time.monotonic() - start < 0.05  # never slept


async def test_paced_waits_the_gap_after_each_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1000.0  # a realistic (large) monotonic value so the first call is free
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        nonlocal now
        sleeps.append(delay)
        now += delay

    monkeypatch.setattr(pymodbus.time, "monotonic", lambda: now)
    monkeypatch.setattr(pymodbus.asyncio, "sleep", fake_sleep)

    conn = PymodbusConnection(None, message_spacing=0.25)  # type: ignore[arg-type]
    async with conn._paced(UNIT_ID):  # first request: runs immediately
        now += 0.10  # ... and occupies the wire for 100 ms
    async with conn._paced(UNIT_ID):  # nothing idle since -> wait the full gap
        pass
    assert sleeps == [pytest.approx(0.25)]


async def test_paced_no_wait_when_already_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1000.0
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        nonlocal now
        sleeps.append(delay)
        now += delay

    monkeypatch.setattr(pymodbus.time, "monotonic", lambda: now)
    monkeypatch.setattr(pymodbus.asyncio, "sleep", fake_sleep)

    conn = PymodbusConnection(None, message_spacing=0.25)  # type: ignore[arg-type]
    async with conn._paced(UNIT_ID):
        pass
    now += 0.50  # caller idled longer than the spacing on its own
    async with conn._paced(UNIT_ID):
        pass
    assert sleeps == []


async def test_paced_serializes_concurrent_callers() -> None:
    """Concurrent callers (the shared-connection case) still line up in order."""
    conn = PymodbusConnection(None, message_spacing=0.02)  # type: ignore[arg-type]

    async def one() -> None:
        async with conn._paced(UNIT_ID):
            pass

    start = time.monotonic()
    await asyncio.gather(*(one() for _ in range(5)))
    # Five requests means four gaps of at least `spacing` each.
    assert time.monotonic() - start >= 0.02 * 4


# -- pymodbus: per-unit spacing on top of the connection-wide gap -------------


def _fake_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Callable[[float], None], list[float]]:
    """Drive pymodbus's clock + sleep off a manual timeline.

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

    monkeypatch.setattr(pymodbus.time, "monotonic", lambda: now)
    monkeypatch.setattr(pymodbus.asyncio, "sleep", fake_sleep)
    return advance, sleeps


def test_negative_unit_spacing_raises() -> None:
    conn = PymodbusConnection(None, message_spacing=0.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        conn.for_unit(UNIT_ID).set_message_spacing(-0.1)


async def test_per_unit_spacing_paces_only_that_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    advance, sleeps = _fake_clock(monkeypatch)
    conn = PymodbusConnection(None, message_spacing=0.0)  # type: ignore[arg-type]
    conn.for_unit(5).set_message_spacing(0.25)

    async with conn._paced(5):  # first request to unit 5: free
        advance(0.10)
    async with conn._paced(5):  # back-to-back on unit 5 -> waits the unit gap
        pass
    async with conn._paced(6):  # a different unit shares the link, not the gap
        pass
    assert sleeps == [pytest.approx(0.25)]  # only unit 5 ever waited


async def test_per_unit_and_link_spacing_take_the_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sleeps = _fake_clock(monkeypatch)
    conn = PymodbusConnection(None, message_spacing=0.05)  # type: ignore[arg-type]
    conn.for_unit(5).set_message_spacing(0.25)

    async with conn._paced(5):  # first request: free
        pass
    async with conn._paced(5):  # waits max(link 0.05, unit 0.25)
        pass
    assert sleeps == [pytest.approx(0.25)]


async def test_clearing_unit_spacing_stops_pacing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    advance, sleeps = _fake_clock(monkeypatch)
    conn = PymodbusConnection(None, message_spacing=0.0)  # type: ignore[arg-type]
    unit = conn.for_unit(5)
    unit.set_message_spacing(0.25)
    async with conn._paced(5):
        advance(0.10)
    unit.set_message_spacing(0)  # cleared -> no more waiting
    async with conn._paced(5):
        pass
    assert sleeps == []


# -- end to end: both backends pace a single unit, across handles -------------


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


# -- tmodbus: forwarded to the native parameter -------------------------------


async def test_tmodbus_forwards_spacing_to_backend() -> None:
    # tmodbus validates wait_between_requests itself; a bad value surfacing proves
    # message_spacing reaches the native parameter.
    with pytest.raises(ValueError):
        await tmodbus_connect_tcp("127.0.0.1", port=502, message_spacing=-0.1)


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
