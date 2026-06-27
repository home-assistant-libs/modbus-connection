"""Minimum inter-request spacing on a shared connection.

Some devices need a quiet gap between consecutive Modbus frames (datasheets call
it ``tx_message_wait``, ``wait_between_requests``, ``message_wait`` ...). With
several consumers sharing one connection, no single consumer can enforce that gap
on its own — it can pace its own calls but not the calls interleaved from the
others. So the gap is enforced one level down, on the shared connection: the
owner opts in at connect time and every request from every unit passes through
one ``MessagePacer``.

This is *spacing between* messages only — the gap after one request before the
next may start. A "wait before the first request" is deliberately not offered:
the owner can simply sleep before issuing its first call.
"""

from __future__ import annotations

import asyncio
import time
from types import TracebackType


class MessagePacer:
    """Serializes requests and holds each one off until the spacing has elapsed.

    Used as an async context manager around each wire request. Entering acquires
    an internal lock — so only one request rides the connection at a time — and
    then sleeps until at least ``spacing`` seconds have passed since the previous
    request finished. Exiting stamps the finish time and releases the lock.

    The gap is measured from completion to start (not start to start), so a
    device's required recovery time is always honored regardless of how long a
    request took, and it is stamped on exit even when the request raised — a
    failed frame still occupied the wire.
    """

    def __init__(self, spacing: float) -> None:
        self._spacing = spacing
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def __aenter__(self) -> MessagePacer:
        await self._lock.acquire()
        delay = self._next_allowed - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._next_allowed = time.monotonic() + self._spacing
        self._lock.release()


def make_pacer(spacing: float) -> MessagePacer | None:
    """Build a pacer for ``spacing`` seconds, or ``None`` when spacing is off.

    Returning ``None`` for the (default) zero case keeps the no-spacing path free
    of any extra lock: the backend's own serialization is left to do its job
    untouched. Raises ``ValueError`` for a negative spacing.
    """
    if spacing < 0:
        raise ValueError("message_spacing must be non-negative")
    return MessagePacer(spacing) if spacing > 0 else None
