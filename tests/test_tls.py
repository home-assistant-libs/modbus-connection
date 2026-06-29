"""connect_tls talks Modbus over TLS (pymodbus only; tmodbus has no TLS).

A self-signed certificate is generated with the ``openssl`` CLI so the test can
stand up a real ``ModbusTlsServer`` and complete an actual TLS handshake.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import ssl
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pymodbus import FramerType
from pymodbus.client import AsyncModbusTlsClient
from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server import ModbusTlsServer

from modbus_connection.pymodbus import connect_tls as pymodbus_connect_tls
from modbus_connection.tmodbus import connect_tls as tmodbus_connect_tls

UNIT_ID = 1

openssl = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl CLI not available"
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _make_cert(directory: Path) -> tuple[str, str]:
    certfile = directory / "cert.pem"
    keyfile = directory / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(keyfile),
            "-out",
            str(certfile),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    return str(certfile), str(keyfile)


@pytest.fixture
async def tls_server(tmp_path: Path) -> AsyncIterator[tuple[str, int]]:
    """A Modbus/TLS server with a self-signed cert and one known register."""
    certfile, keyfile = _make_cert(tmp_path)
    values = [0] * 10
    values[0] = 5579
    device = ModbusDeviceContext(ir=ModbusSequentialDataBlock(1, values))
    context = ModbusServerContext(devices=device)
    host, port = "127.0.0.1", _free_port()
    server = ModbusTlsServer(
        context,
        framer=FramerType.TLS,
        address=(host, port),
        certfile=certfile,
        keyfile=keyfile,
    )
    task = asyncio.create_task(server.serve_forever())
    await asyncio.sleep(0.4)
    try:
        yield host, port
    finally:
        await server.shutdown()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


@openssl
async def test_pymodbus_tls_reads(tls_server: tuple[str, int]) -> None:
    host, port = tls_server
    # The server cert is self-signed: use a non-verifying client context.
    sslctx = AsyncModbusTlsClient.generate_ssl()
    sslctx.check_hostname = False
    sslctx.verify_mode = ssl.CERT_NONE
    conn = await pymodbus_connect_tls(host, port=port, sslctx=sslctx)
    try:
        assert conn.connected is True
        assert await conn.for_unit(UNIT_ID).read_holding_registers(0, 1) == [5579]
    finally:
        await conn.close()


async def test_tmodbus_tls_not_implemented() -> None:
    """tmodbus ships no TLS transport: connect_tls raises NotImplementedError."""
    with pytest.raises(NotImplementedError):
        await tmodbus_connect_tls("127.0.0.1", port=802)
