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
from pymodbus import ModbusDeviceIdentification
from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server import ModbusTcpServer

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


def _device_identity() -> ModbusDeviceIdentification:
    ident = ModbusDeviceIdentification()
    ident.VendorName = DEVICE_ID[0].decode()
    ident.ProductCode = DEVICE_ID[1].decode()
    ident.MajorMinorRevision = DEVICE_ID[2].decode()
    return ident


def _block_from(
    mapping: dict[int, int | bool], size: int = 2200
) -> ModbusSequentialDataBlock:
    # ModbusSequentialDataBlock(address, values) stores values starting at the
    # 1-based `address` (internally SimData(address - 1)), so block address 1 maps
    # protocol register N to values[N]. Its datatype packs each word as a *signed*
    # 16-bit int, so store the two's-complement equivalent (same bytes on the wire,
    # so unsigned reads round-trip unchanged); coil 0/1 values are unaffected.
    values = [0] * (size + 1)
    for address, value in mapping.items():
        word = int(value) & 0xFFFF
        values[address] = word - 0x10000 if word >= 0x8000 else word
    return ModbusSequentialDataBlock(1, values)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def modbus_server() -> AsyncIterator[tuple[str, int]]:
    """Start a Modbus TCP server with the known datastore; yield (host, port)."""
    # pymodbus 3.13's deprecated ModbusDeviceContext crosses input and holding:
    # FC03 (holding) is served from the `ir` slot and FC04 (input) from `hr`, so
    # pass the blocks swapped to land each on the right function code.
    device = ModbusDeviceContext(
        di=_block_from(DISCRETE),
        co=_block_from(COILS),
        ir=_block_from(HOLDING),
        hr=_block_from(INPUT),
    )
    # Pass the device directly (not a {id: device} dict): the dict path of the
    # deprecated ModbusServerContext mis-wires SimCore in pymodbus 3.13. A single
    # device is registered at id 0 and served for any unit id (incl. UNIT_ID).
    context = ModbusServerContext(devices=device)
    host, port = "127.0.0.1", _free_port()
    server = ModbusTcpServer(context, identity=_device_identity(), address=(host, port))
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
