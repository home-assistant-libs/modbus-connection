"""Shared test fixtures: a real in-process Modbus TCP server.

Both backends connect to the *same* server, so the test suite validates real
end-to-end behavior and cross-backend parity rather than mock interactions.
"""

from __future__ import annotations

import asyncio
import socket
import struct
from collections.abc import AsyncIterator

import pytest
from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server import ModbusTcpServer

UNIT_ID = 1

# Known holding-register contents. Addresses chosen to exercise every typed read.
#   0  : uint16 1234
#   1  : 0xFFFF -> int16 -1
#   2-3: uint32 (big) 70000
#   4-5: float32 12.5
#   6-9: string "AB CD"
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


def _block_from(
    mapping: dict[int, int | bool], size: int = 2200
) -> ModbusSequentialDataBlock:
    # pymodbus' datastore is 1-based: a protocol read of address N hits block
    # index N+1. Shift values right by one so protocol address N returns the
    # value we mapped to N.
    values = [0] * (size + 1)
    for address, value in mapping.items():
        values[address + 1] = int(value)
    return ModbusSequentialDataBlock(0, values)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def modbus_server() -> AsyncIterator[tuple[str, int]]:
    """Start a Modbus TCP server with the known datastore; yield (host, port)."""
    device = ModbusDeviceContext(
        di=_block_from(DISCRETE),
        co=_block_from(COILS),
        ir=_block_from(INPUT),
        hr=_block_from(HOLDING),
    )
    context = ModbusServerContext(devices={UNIT_ID: device}, single=False)
    host, port = "127.0.0.1", _free_port()
    server = ModbusTcpServer(context, address=(host, port))
    task = asyncio.create_task(server.serve_forever())
    # Wait until the listener is actually accepting connections.
    for _ in range(100):
        try:
            reader, writer = await asyncio.open_connection(host, port)
        except OSError:
            await asyncio.sleep(0.02)
            continue
        writer.close()
        await writer.wait_closed()
        break
    try:
        yield host, port
    finally:
        await server.shutdown()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
