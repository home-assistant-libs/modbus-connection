"""connect_tls talks Modbus over TLS on both backends.

A self-signed certificate is generated with the ``openssl`` CLI so the test can
stand up a real ``ModbusTlsServer`` and complete an actual TLS handshake against
either backend.
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

from modbus_connection import ModbusConnection, ModbusError
from modbus_connection.pymodbus import connect_tls as pymodbus_connect_tls
from modbus_connection.tmodbus import connect_tls as tmodbus_connect_tls

UNIT_ID = 1

BACKENDS = ["pymodbus", "tmodbus"]


def _connect_tls(backend: str, host: str, **kwargs: object) -> object:
    """Return the awaitable connect_tls for the chosen backend."""
    connect = pymodbus_connect_tls if backend == "pymodbus" else tmodbus_connect_tls
    return connect(host, **kwargs)  # type: ignore[arg-type]


backends = pytest.mark.parametrize("backend", BACKENDS)

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
            # SAN so hostname verification passes when a client pins this cert.
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return str(certfile), str(keyfile)


@pytest.fixture
async def tls_server(tmp_path: Path) -> AsyncIterator[tuple[str, int, str]]:
    """A Modbus/TLS server with a self-signed cert; yields (host, port, certfile)."""
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
        yield host, port, certfile
    finally:
        await server.shutdown()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


@openssl
@backends
async def test_tls_explicit_sslctx_overrides_verify(
    backend: str, tls_server: tuple[str, int, str]
) -> None:
    """A caller-supplied sslctx takes precedence over verify."""
    host, port, _ = tls_server
    sslctx = AsyncModbusTlsClient.generate_ssl()
    sslctx.check_hostname = False
    sslctx.verify_mode = ssl.CERT_NONE
    conn: ModbusConnection = await _connect_tls(backend, host, port=port, sslctx=sslctx)
    try:
        assert conn.connected is True
        assert await conn.for_unit(UNIT_ID).read_holding_registers(0, 1) == [5579]
    finally:
        await conn.close()


@openssl
@backends
async def test_tls_verifies_by_default(
    backend: str, tls_server: tuple[str, int, str]
) -> None:
    """The default (verify=True) rejects a server whose cert isn't trusted."""
    host, port, _ = tls_server
    with pytest.raises(ModbusError):
        await _connect_tls(backend, host, port=port, timeout=1)


@openssl
@backends
async def test_tls_verify_false_connects(
    backend: str, tls_server: tuple[str, int, str]
) -> None:
    """verify=False accepts a self-signed server without an explicit sslctx."""
    host, port, _ = tls_server
    conn: ModbusConnection = await _connect_tls(backend, host, port=port, verify=False)
    try:
        assert await conn.for_unit(UNIT_ID).read_holding_registers(0, 1) == [5579]
    finally:
        await conn.close()


@openssl
@backends
async def test_tls_verify_with_pinned_cafile(
    backend: str, tls_server: tuple[str, int, str]
) -> None:
    """verify=<path> pins the device's own cert as the CA to verify against."""
    host, port, certfile = tls_server
    conn: ModbusConnection = await _connect_tls(
        backend, host, port=port, verify=certfile
    )
    try:
        assert await conn.for_unit(UNIT_ID).read_holding_registers(0, 1) == [5579]
    finally:
        await conn.close()


def test_build_tls_context_hostname_and_verify_flags() -> None:
    """check_hostname toggles name matching without dropping cert verification."""
    from modbus_connection._tls import build_tls_context

    verifying = build_tls_context(True, True, None, None, None)
    assert verifying.check_hostname is True
    assert verifying.verify_mode is ssl.CERT_REQUIRED

    no_hostname = build_tls_context(True, False, None, None, None)
    assert no_hostname.check_hostname is False
    assert no_hostname.verify_mode is ssl.CERT_REQUIRED  # still verifies the cert

    unverified = build_tls_context(False, True, None, None, None)
    assert unverified.check_hostname is False  # check_hostname ignored
    assert unverified.verify_mode is ssl.CERT_NONE
