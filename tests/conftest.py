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
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

from modbus_connection import ModbusConnection

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

# Addresses outside a SimData block answer with an exception, so make the
# blocks comfortably larger than any address the tests touch.
_BLOCK_SIZE = 2200


def _register_block(mapping: dict[int, int]) -> list[SimData]:
    """A register block: ``_BLOCK_SIZE`` words, zero unless set in ``mapping``."""
    values = [0] * _BLOCK_SIZE
    for address, value in mapping.items():
        values[address] = int(value) & 0xFFFF
    return [SimData(0, values=values, datatype=DataType.REGISTERS)]


def _bit_block(mapping: dict[int, bool]) -> list[SimData]:
    """A coil / discrete-input block, one bool per address."""
    values = [False] * _BLOCK_SIZE
    for address, value in mapping.items():
        values[address] = bool(value)
    return [SimData(0, values=values, datatype=DataType.BITS)]


def sim_holding_device(values: list[int]) -> SimDevice:
    """A device serving ``values`` as holding registers 0..len-1.

    The shared datastore for the transport tests (UDP, serial, TLS, framer
    variants), which only need a couple of known holding registers. ``id=0``
    serves every unit id.
    """
    words = [v & 0xFFFF for v in values]
    holding = [SimData(0, values=words, datatype=DataType.REGISTERS)]
    return SimDevice(
        0,
        simdata=(_bit_block({}), _bit_block({}), holding, _register_block({})),
    )


def _device_identity() -> ModbusDeviceIdentification:
    ident = ModbusDeviceIdentification()
    ident.VendorName = DEVICE_ID[0].decode()
    ident.ProductCode = DEVICE_ID[1].decode()
    ident.MajorMinorRevision = DEVICE_ID[2].decode()
    return ident


async def drop_link(conn: ModbusConnection) -> None:
    """Down a live connection the way a transport drop does.

    Stands in for a real link loss (which both backends report through their
    connection-lost hook): the client is cleared and torn down, so the link is
    down and the next request has to reconnect.
    """
    client, conn._client = conn._client, None
    await conn._close_client(client)


@pytest.fixture
def free_port() -> int:
    """A port on localhost that nothing is listening on."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def free_udp_port() -> int:
    """A UDP port on localhost that nothing is bound to."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def modbus_server(free_port: int) -> AsyncIterator[tuple[str, int]]:
    """Start a Modbus TCP server with the known datastore; yield (host, port)."""
    # Non-shared blocks, ordered (coils, discrete inputs, holding, input).
    # id=0 serves every unit id (incl. UNIT_ID).
    device = SimDevice(
        0,
        simdata=(
            _bit_block(COILS),
            _bit_block(DISCRETE),
            _register_block(HOLDING),
            _register_block(INPUT),
        ),
    )
    host, port = "127.0.0.1", free_port
    server = ModbusTcpServer(device, identity=_device_identity(), address=(host, port))
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
