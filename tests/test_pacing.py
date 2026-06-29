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
        async with conn._paced():
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
    async with conn._paced():  # first request: runs immediately
        now += 0.10  # ... and occupies the wire for 100 ms
    async with conn._paced():  # nothing idle since -> wait the full gap
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
    async with conn._paced():
        pass
    now += 0.50  # caller idled longer than the spacing on its own
    async with conn._paced():
        pass
    assert sleeps == []


async def test_paced_serializes_concurrent_callers() -> None:
    """Concurrent callers (the shared-connection case) still line up in order."""
    conn = PymodbusConnection(None, message_spacing=0.02)  # type: ignore[arg-type]

    async def one() -> None:
        async with conn._paced():
            pass

    start = time.monotonic()
    await asyncio.gather(*(one() for _ in range(5)))
    # Five requests means four gaps of at least `spacing` each.
    assert time.monotonic() - start >= 0.02 * 4


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
    # Four requests means three gaps of at least `spacing` each.
    assert elapsed >= spacing * 3
