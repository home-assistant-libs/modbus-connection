"""ModbusTlsParams talks Modbus over TLS on both backends' clients.

A self-signed certificate is generated with the ``openssl`` CLI so the test can
stand up a real Modbus/TCP Security (mbaps) server and complete an actual TLS
handshake.
"""

from __future__ import annotations

import asyncio
import shutil
import ssl
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType

import pytest

import modbus_connection.pymodbus as pymodbus_backend
import modbus_connection.tmodbus as tmodbus_backend
from modbus_connection import ModbusError, ModbusTlsParams
from modbus_connection._client import BaseModbusConnection

from .modbus_server import holding_store, serve_tcp

UNIT_ID = 1

backends = pytest.mark.parametrize(
    "client_cls",
    [pymodbus_backend.ModbusConnection, tmodbus_backend.ModbusConnection],
    ids=["pymodbus", "tmodbus"],
)

backend_modules = pytest.mark.parametrize(
    "backend",
    [pymodbus_backend, tmodbus_backend],
    ids=["pymodbus", "tmodbus"],
)

openssl = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl CLI not available"
)


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
async def tls_server(
    free_port: int, tmp_path: Path
) -> AsyncIterator[tuple[str, int, str]]:
    """A Modbus/TLS server with a self-signed cert; yields (host, port, certfile)."""
    certfile, keyfile = _make_cert(tmp_path)
    values = [0] * 10
    values[0] = 5579
    host, port = "127.0.0.1", free_port
    server_sslctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_sslctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
    async with serve_tcp(holding_store(values), host, port, ssl_context=server_sslctx):
        yield host, port, certfile


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
async def test_tls_explicit_sslctx_is_reusable_across_connections(
    client_cls: type[BaseModbusConnection], tls_server: tuple[str, int, str]
) -> None:
    """One caller-owned SSLContext can serve multiple lazy connections."""
    host, port, certfile = tls_server
    sslctx = ssl.create_default_context(cafile=certfile)
    params = ModbusTlsParams(host=host, port=port, sslctx=sslctx)
    clients = [client_cls(params), client_cls(params)]
    try:
        assert all(client.connected is False for client in clients)
        assert await asyncio.gather(
            *(
                client.for_unit(UNIT_ID).read_holding_registers(0, 1)
                for client in clients
            )
        ) == [[5579], [5579]]
        assert all(client.connected is True for client in clients)
        assert params.sslctx is sslctx
        hash(params)
    finally:
        await asyncio.gather(*(client.close() for client in clients))


@openssl
@backend_modules
async def test_connect_tls_accepts_sslctx(
    backend: ModuleType, tls_server: tuple[str, int, str]
) -> None:
    """The eager factory keeps its pre-existing sslctx keyword."""
    host, port, certfile = tls_server
    sslctx = ssl.create_default_context(cafile=certfile)
    client = await backend.connect_tls(host, port=port, sslctx=sslctx)
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


async def test_create_ssl_context_hostname_and_verify_flags() -> None:
    """check_hostname toggles name matching without dropping cert verification."""
    verifying = await ModbusTlsParams(host="device.local").create_ssl_context()
    assert verifying.check_hostname is True
    assert verifying.verify_mode is ssl.CERT_REQUIRED

    no_hostname = await ModbusTlsParams(
        host="device.local", check_hostname=False
    ).create_ssl_context()
    assert no_hostname.check_hostname is False
    assert no_hostname.verify_mode is ssl.CERT_REQUIRED  # still verifies the cert

    unverified = await ModbusTlsParams(
        host="device.local", verify=False
    ).create_ssl_context()
    assert unverified.check_hostname is False  # check_hostname ignored
    assert unverified.verify_mode is ssl.CERT_NONE
