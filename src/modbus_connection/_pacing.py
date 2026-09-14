"""Pace requests on a Modbus connection."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import NamedTuple

from ._types import SpacingBasis


class _UnitGap(NamedTuple):
    """A unit's gap and what it is measured from."""

    seconds: float
    basis: SpacingBasis


class Pacer:
    """Serializes requests on one connection and enforces the gaps between them."""

    def __init__(self, message_spacing: float = 0.0) -> None:
        if message_spacing < 0:
            raise ValueError("message_spacing must be non-negative")
        self._message_spacing = message_spacing
        self._lock = asyncio.Lock()
        self._last_finished_at = 0.0
        self._unit_spacing: dict[int, _UnitGap] = {}
        self._unit_last_finished_at: dict[int, float] = {}

    def set_unit_spacing(
        self, unit_id: int, seconds: float, since: SpacingBasis = "unit"
    ) -> None:
        """Set (or, with ``0``, clear) the per-unit gap for ``unit_id``.

        ``since`` selects what the gap is measured from. ``"unit"`` measures
        from the last request to this unit. ``"connection"`` measures from the
        last request on the connection, whichever unit it addressed.
        """
        if seconds < 0:
            raise ValueError("message_spacing must be non-negative")
        if since not in ("unit", "connection"):
            raise ValueError("since must be 'unit' or 'connection'")
        if seconds:
            self._unit_spacing[unit_id] = _UnitGap(seconds, since)
        else:
            self._unit_spacing.pop(unit_id, None)
            self._unit_last_finished_at.pop(unit_id, None)

    @asynccontextmanager
    async def exclusive(self, timeout: float) -> AsyncIterator[None]:
        """Hold the connection for teardown, or proceed after ``timeout``.

        A request that is about to answer gets to deliver its result. A wedged
        one must not hold the teardown up, so the wait is bounded.
        """
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout)
        except TimeoutError:
            yield
            return
        try:
            yield
        finally:
            self._lock.release()

    @asynccontextmanager
    async def paced(self, unit_id: int) -> AsyncIterator[None]:
        """Hold the connection for one request, after the configured gaps."""
        async with self._lock:
            gap = self._unit_spacing.get(unit_id)
            if self._message_spacing or gap:
                now = time.monotonic()
                wait = self._message_spacing - (now - self._last_finished_at)
                if gap:
                    # The lock is held across the sleep, so no other unit can
                    # put a frame on the line during a gap measured from it.
                    last = (
                        self._last_finished_at
                        if gap.basis == "connection"
                        else self._unit_last_finished_at.get(unit_id, 0.0)
                    )
                    wait = max(wait, gap.seconds - (now - last))
                if wait > 0:
                    await asyncio.sleep(wait)
            try:
                yield
            finally:
                finished = time.monotonic()
                self._last_finished_at = finished
                if gap:
                    self._unit_last_finished_at[unit_id] = finished
