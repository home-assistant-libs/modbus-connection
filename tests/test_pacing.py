"""Tests for inter-request spacing (``message_spacing``).

The slot-math test drives a fake monotonic clock for a deterministic assertion;
the rest use real (small) timing to prove the backends pace concurrent requests
across units and that the connect functions wire the parameter through.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from modbus_connection import pymodbus
from modbus_connection.pymodbus import PymodbusConnection
from modbus_connection.pymodbus import connect_tcp as pymodbus_connect_tcp
from modbus_connection.tmodbus import connect_tcp as tmodbus_connect_tcp

from .conftest import UNIT_ID


def test_negative_spacing_raises() -> None:
    with pytest.raises(ValueError):
        PymodbusConnection(None, message_spacing=-0.1)  # type: ignore[arg-type]


async def test_pace_is_noop_when_disabled() -> None:
    conn = PymodbusConnection(None, message_spacing=0.0)  # type: ignore[arg-type]
    start = time.monotonic()
    for _ in range(5):
        await conn._pace()
    assert time.monotonic() - start < 0.05  # never slept


async def test_pace_reserves_evenly_spaced_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        nonlocal now
        sleeps.append(delay)
        now += delay

    monkeypatch.setattr(pymodbus.time, "monotonic", lambda: now)
    monkeypatch.setattr(pymodbus.asyncio, "sleep", fake_sleep)

    conn = PymodbusConnection(None, message_spacing=0.25)  # type: ignore[arg-type]
    for _ in range(3):
        await conn._pace()
    # First request runs immediately; each later one waits a full interval.
    assert sleeps == [pytest.approx(0.25), pytest.approx(0.25)]


async def test_pace_serializes_concurrent_callers() -> None:
    """Concurrent callers (the shared-connection case) still line up in order."""
    conn = PymodbusConnection(None, message_spacing=0.02)  # type: ignore[arg-type]
    start = time.monotonic()
    await asyncio.gather(*(conn._pace() for _ in range(5)))
    # Five slots 0.02 apart -> the last waits ~0.08s.
    assert time.monotonic() - start >= 0.02 * 4


@pytest.mark.parametrize("backend", ["pymodbus", "tmodbus"])
async def test_backend_paces_requests(
    modbus_server: tuple[str, int], backend: str
) -> None:
    host, port = modbus_server
    spacing = 0.05
    if backend == "pymodbus":
        conn = await pymodbus_connect_tcp(host, port=port, message_spacing=spacing)
    else:
        conn = await tmodbus_connect_tcp(
            host, port=port, unit_id=UNIT_ID, message_spacing=spacing
        )
    try:
        unit = conn.for_unit(UNIT_ID)
        start = time.monotonic()
        for _ in range(4):
            await unit.read_holding_registers(0, 1)
        elapsed = time.monotonic() - start
    finally:
        await conn.close()
    # Four requests means three intervals of at least `spacing` each.
    assert elapsed >= spacing * 3
