"""ModbusTlsParams talks Modbus over TLS on both backends' clients.

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
from pymodbus.server import ModbusTlsServer

import modbus_connection.pymodbus as pymodbus_backend
import modbus_connection.tmodbus as tmodbus_backend
from modbus_connection import ModbusError, ModbusTlsParams
from modbus_connection._client import BaseModbusConnection

from .conftest import sim_holding_device

UNIT_ID = 1

backends = pytest.mark.parametrize(
    "client_cls",
    [pymodbus_backend.ModbusConnection, tmodbus_backend.ModbusConnection],
    ids=["pymodbus", "tmodbus"],
)

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
    context = sim_holding_device(values)
    host, port = "127.0.0.1", _free_port()
    server_sslctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_sslctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
    server = ModbusTlsServer(
        context,
        framer=FramerType.TLS,
        address=(host, port),
        sslctx=server_sslctx,
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
async def test_tls_verify_with_pinned_cafile(
    client_cls: type[BaseModbusConnection], tls_server: tuple[str, int, str]
) -> None:
    """verify=<path> pins the device's own cert as the CA to verify against.

    The client is lazy: the certificate context is only built (and the socket
    opened) on the first request.
    """
    host, port, certfile = tls_server
    client = client_cls(ModbusTlsParams(host=host, port=port, verify=certfile))
    try:
        assert client.connected is False
        assert await client.for_unit(UNIT_ID).read_holding_registers(0, 1) == [5579]
        assert client.connected is True
    finally:
        await client.close()


@openssl
@backends
async def test_tls_verify_false_connects(
    client_cls: type[BaseModbusConnection], tls_server: tuple[str, int, str]
) -> None:
    """verify=False accepts a self-signed server."""
    host, port, _ = tls_server
    client = client_cls(ModbusTlsParams(host=host, port=port, verify=False))
    try:
        assert await client.for_unit(UNIT_ID).read_holding_registers(0, 1) == [5579]
    finally:
        await client.close()


@openssl
@backends
async def test_tls_verifies_by_default(
    client_cls: type[BaseModbusConnection], tls_server: tuple[str, int, str]
) -> None:
    """The default (verify=True) rejects a server whose cert isn't trusted —
    surfaced on the first request, not at construction."""
    host, port, _ = tls_server
    client = client_cls(ModbusTlsParams(host=host, port=port), timeout=1)
    try:
        with pytest.raises(ModbusError):
            await client.for_unit(UNIT_ID).read_holding_registers(0, 1)
    finally:
        await client.close()


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
