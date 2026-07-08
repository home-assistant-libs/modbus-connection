"""The shared inter-request pacer, used by every backend.

A backend has no native inter-request pacing we rely on: pymodbus has no such
knob, and while tmodbus does, we enforce spacing here instead so both backends
behave identically. One :class:`Pacer` lives on each connection.

Two gaps compose. A **connection-wide** gap (``message_spacing``, measured since
the last request on the link finished) paces the whole link; an optional
**per-unit** gap (measured since the last request to the same unit finished)
paces one device. A request waits for whichever is longer. A single lock makes
the read-wait-update atomic across every unit sharing the link, so the gap holds
no matter which unit issues the next request.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class Pacer:
    """Enforces the connection-wide and per-unit gaps for one connection."""

    def __init__(self, message_spacing: float = 0.0) -> None:
        if message_spacing < 0:
            raise ValueError("message_spacing must be non-negative")
        self._message_spacing = message_spacing
        self._lock = asyncio.Lock()
        self._last_finished_at = 0.0
        self._unit_spacing: dict[int, float] = {}
        self._unit_last_finished_at: dict[int, float] = {}

    def set_unit_spacing(self, unit_id: int, seconds: float) -> None:
        """Set (or, with ``0``, clear) the per-unit gap for ``unit_id``."""
        if seconds < 0:
            raise ValueError("message_spacing must be non-negative")
        if seconds:
            self._unit_spacing[unit_id] = seconds
        else:
            self._unit_spacing.pop(unit_id, None)
            self._unit_last_finished_at.pop(unit_id, None)

    @asynccontextmanager
    async def paced(self, unit_id: int) -> AsyncIterator[None]:
        """Hold a request to ``unit_id`` until both the connection-wide gap and
        the unit's own gap have elapsed, then measure both from its completion.

        No-op when neither the link nor the unit has spacing configured.
        """
        unit_spacing = self._unit_spacing.get(unit_id, 0.0)
        if not self._message_spacing and not unit_spacing:
            yield
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._message_spacing - (now - self._last_finished_at)
            if unit_spacing:
                last_unit = self._unit_last_finished_at.get(unit_id, 0.0)
                wait = max(wait, unit_spacing - (now - last_unit))
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                yield
            finally:
                finished = time.monotonic()
                self._last_finished_at = finished
                if unit_spacing:
                    self._unit_last_finished_at[unit_id] = finished
