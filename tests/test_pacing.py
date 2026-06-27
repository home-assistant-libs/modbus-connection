"""Tests for inter-request spacing (``message_spacing``).

The ``MessagePacer`` tests drive a fake monotonic clock so the spacing logic is
asserted deterministically; the backend tests use real (small) timing to prove
the connect functions actually route every request through the pacer.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import pytest

from modbus_connection import _pacing
from modbus_connection._pacing import MessagePacer, make_pacer
from modbus_connection.pymodbus import connect_tcp as pymodbus_connect_tcp
from modbus_connection.tmodbus import connect_tcp as tmodbus_connect_tcp

from .conftest import UNIT_ID

# -- make_pacer ---------------------------------------------------------------


def test_make_pacer_zero_is_disabled() -> None:
    assert make_pacer(0.0) is None


def test_make_pacer_positive_builds_pacer() -> None:
    assert isinstance(make_pacer(0.25), MessagePacer)


def test_make_pacer_negative_raises() -> None:
    with pytest.raises(ValueError):
        make_pacer(-0.1)


# -- MessagePacer spacing logic (deterministic, fake clock) -------------------


@dataclass
class FakeClock:
    """A virtual monotonic clock; ``advance`` moves "now" forward by hand."""

    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def advance(self, delta: float) -> None:
        self.now += delta


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Replace the pacer's ``time.monotonic`` / ``asyncio.sleep`` with a fake clock.

    ``asyncio.sleep`` advances the clock instead of waiting and records each
    requested delay on ``clock.sleeps``, so spacing is asserted deterministically.
    """
    fake = FakeClock()

    async def fake_sleep(delay: float) -> None:
        fake.sleeps.append(delay)
        fake.advance(delay)

    monkeypatch.setattr(_pacing.time, "monotonic", lambda: fake.now)
    monkeypatch.setattr(_pacing.asyncio, "sleep", fake_sleep)
    return fake


async def test_first_request_never_waits(clock: FakeClock) -> None:
    pacer = MessagePacer(0.25)
    async with pacer:
        pass
    assert clock.sleeps == []


async def test_gap_measured_from_completion_to_start(clock: FakeClock) -> None:
    pacer = MessagePacer(0.25)
    async with pacer:
        clock.advance(0.10)  # the request occupies the wire for 100 ms
    async with pacer:  # nothing idle since it finished -> wait the full gap
        pass
    assert clock.sleeps == [pytest.approx(0.25)]


async def test_no_wait_when_gap_already_elapsed(clock: FakeClock) -> None:
    pacer = MessagePacer(0.25)
    async with pacer:
        pass
    clock.advance(0.50)  # caller idled longer than the spacing on its own
    async with pacer:
        pass
    assert clock.sleeps == []


async def test_spacing_stamped_even_when_request_raises(clock: FakeClock) -> None:
    pacer = MessagePacer(0.25)
    with pytest.raises(RuntimeError):
        async with pacer:
            clock.advance(0.05)
            raise RuntimeError("boom")
    async with pacer:  # the failed frame still occupied the wire -> still spaced
        pass
    assert clock.sleeps == [pytest.approx(0.25)]


# -- serialization ------------------------------------------------------------


async def test_pacer_serializes_concurrent_requests() -> None:
    """Only one request rides the connection at a time, even under concurrency."""
    pacer = MessagePacer(0.0)
    active = 0
    max_active = 0

    async def worker() -> None:
        nonlocal active, max_active
        async with pacer:
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0)  # hand control to the other tasks
            active -= 1

    await asyncio.gather(*(worker() for _ in range(5)))
    assert max_active == 1


# -- end-to-end: the backends honor message_spacing ---------------------------


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
    # Four requests means three inter-request gaps of at least `spacing` each.
    assert elapsed >= spacing * 3
