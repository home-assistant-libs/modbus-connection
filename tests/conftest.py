"""Shared test data and fixtures.

Both backends connect to the *same* tmodbus server (see ``modbus_server``), so
the suite validates real end-to-end behavior and cross-backend parity rather
than mock interactions. Running pymodbus's client against tmodbus's server also
keeps the cross-implementation coverage a same-library loop would lose.
"""

from __future__ import annotations

import socket
import struct
import subprocess
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from modbus_connection import ModbusConnection

from .modbus_server import Datastore, serve_tcp

UNIT_ID = 1

# Known holding-register contents, shared by the raw-read and parity tests.
HOLDING: dict[int, int] = {0: 1234, 1: 0xFFFF}
HOLDING[2], HOLDING[3] = (70000 >> 16) & 0xFFFF, 70000 & 0xFFFF
_f = struct.unpack(">HH", struct.pack(">f", 12.5))
HOLDING[4], HOLDING[5] = _f[0], _f[1]
for i, ch in enumerate(b"ABCDEF\x00\x00"):
    reg, hi = divmod(i, 2)
    HOLDING.setdefault(6 + reg, 0)
    HOLDING[6 + reg] |= ch << (8 if hi == 0 else 0)

INPUT: dict[int, int] = {0: 555, 1: 777}
COILS: dict[int, bool] = {0: True, 1: False, 2: True, 56: True}
DISCRETE: dict[int, bool] = {0: False, 1: True, 2: True}

# Device identification (FC43/14) the server advertises, keyed by MEI object id
# (0 VendorName, 1 ProductCode, 2 MajorMinorRevision).
DEVICE_ID: dict[int, bytes] = {0: b"Acme", 1: b"PC-1", 2: b"1.2"}

# Diagnostics (FC08 sub-function 0x0B) and comm-event (FC0B/FC0C) contents.
BUS_MESSAGE_COUNT = 42
COMM_EVENT_LOG = b"\x20\x00\x40"


def full_store() -> Datastore:
    return Datastore(
        holding=dict(HOLDING),
        input=dict(INPUT),
        coils=dict(COILS),
        discrete_inputs=dict(DISCRETE),
        device_id=dict(DEVICE_ID),
        bus_message_count=BUS_MESSAGE_COUNT,
        comm_event_log=COMM_EVENT_LOG,
    )


async def drop_link(conn: ModbusConnection) -> None:
    """Down a live connection the way a transport drop does.

    The client is cleared and torn down, so the link is down and the next
    request has to reconnect.
    """
    client, conn._client = conn._client, None
    await conn._close_client(client)


@contextmanager
def _bound_port(kind: int) -> Iterator[int]:
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        yield sock.getsockname()[1]


@pytest.fixture
def free_port() -> int:
    with _bound_port(socket.SOCK_STREAM) as port:
        return port


@pytest.fixture
def free_udp_port() -> int:
    with _bound_port(socket.SOCK_DGRAM) as port:
        return port


@pytest.fixture
async def modbus_server(free_port: int) -> AsyncIterator[tuple[str, int]]:
    """A Modbus TCP server with the known datastore; yields (host, port)."""
    host, port = "127.0.0.1", free_port
    async with serve_tcp(full_store(), host, port):
        yield host, port


@pytest.fixture(scope="session")
def official_models(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The official SunSpec model definitions, cloned once for the session."""
    repo = tmp_path_factory.mktemp("sunspec") / "models"
    clone = subprocess.run(  # noqa: S603
        [
            "git",
            "clone",
            "--depth",
            "1",
            "https://github.com/sunspec/models",
            str(repo),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if clone.returncode != 0:
        pytest.skip(f"cannot clone sunspec/models: {clone.stderr.strip()[:200]}")
    return Path(repo, "json")
