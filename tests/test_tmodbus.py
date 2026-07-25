"""tmodbus-backend unit tests that don't need a live server."""

from __future__ import annotations

import pytest
from tmodbus.exceptions import InvalidResponseError
from tmodbus.exceptions import ModbusConnectionError as TModbusConnectionError

import modbus_connection.tmodbus as tmodbus_backend
from modbus_connection import (
    ModbusConnectionError,
    ModbusProtocolError,
    ModbusTcpParams,
)
from modbus_connection.tmodbus import ModbusConnection, TmodbusUnit


class _FakeClientBase:
    """A minimal stand-in for the tmodbus base client, always 'connected'."""

    connected = True

    def for_unit_id(self, unit_id: int) -> _FakeClientBase:
        return self

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass


def _client_over(
    monkeypatch: pytest.MonkeyPatch, fake_client: _FakeClientBase
) -> ModbusConnection:
    """A ModbusConnection whose tmodbus base client is ``fake_client``."""
    monkeypatch.setattr(
        tmodbus_backend, "create_async_tcp_client", lambda *a, **k: fake_client
    )
    return ModbusConnection(ModbusTcpParams(host="test"))


def _unit_over(
    monkeypatch: pytest.MonkeyPatch, fake_client: _FakeClientBase
) -> TmodbusUnit:
    """A TmodbusUnit whose owning ModbusConnection wraps ``fake_client``."""
    return _client_over(monkeypatch, fake_client).for_unit(1)


class _FakeFileClient(_FakeClientBase):
    """A stand-in tmodbus client that records file-record calls."""

    def __init__(self, read_data: bytes = b"") -> None:
        self._read_data = read_data
        self.read_calls: list[tuple[int, int, int]] = []
        self.write_calls: list[tuple[int, int, bytes]] = []

    async def read_file_record(
        self, file_number: int, record_number: int, record_length: int
    ) -> bytes:
        self.read_calls.append((file_number, record_number, record_length))
        return self._read_data

    async def write_file_record(
        self, file_number: int, record_number: int, data: bytes
    ) -> object:
        self.write_calls.append((file_number, record_number, data))
        return object()


async def test_read_file_record_decodes_to_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeFileClient(b"\x00\x2a\x01\x00")  # words 42 and 256
    unit = _unit_over(monkeypatch, client)

    words = await unit.read_file_record(file=4, record=1, length=2)

    assert words == [42, 256]
    assert client.read_calls == [(4, 1, 2)]


async def test_write_file_record_encodes_words_to_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeFileClient()
    unit = _unit_over(monkeypatch, client)

    await unit.write_file_record(file=7, record=9, values=[42, 256])

    assert client.write_calls == [(7, 9, b"\x00\x2a\x01\x00")]


class _InvalidResponseClient(_FakeClientBase):
    """A unit client whose reads always fail with an invalid response."""

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        raise InvalidResponseError("bad CRC", response_bytes=b"\x00")


async def test_invalid_response_maps_to_protocol_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unit = _unit_over(monkeypatch, _InvalidResponseClient())

    with pytest.raises(ModbusProtocolError):
        await unit.read_holding_registers(0, 1)


class _DroppingClient(_FakeClientBase):
    """A unit client whose reads always fail as a lost connection."""

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        raise TModbusConnectionError("link down")


async def test_request_failure_maps_but_does_not_fire_on_connection_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Loss is reported by the transport's on_connection_lost hook, not by a failed
    # request, so a request that hits a dropped link only translates the error.
    conn = _client_over(monkeypatch, _DroppingClient())
    calls: list[int] = []
    conn.on_connection_lost(lambda: calls.append(1))
    unit = conn.for_unit(1)

    for _ in range(3):
        with pytest.raises(ModbusConnectionError):
            await unit.read_holding_registers(0, 1)

    assert calls == []


async def test_transport_hook_fires_registered_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _client_over(monkeypatch, _FakeClientBase())
    calls: list[int] = []
    conn.on_connection_lost(lambda: calls.append(1))

    conn._on_connection_lost(TModbusConnectionError("link down"))

    assert calls == [1]


async def test_close_suppresses_on_connection_lost_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A deliberate close() also triggers tmodbus's on_connection_lost hook (with a
    # None cause); that is not a lost connection, so it must not fire callbacks.
    conn = _client_over(monkeypatch, _FakeClientBase())
    calls: list[int] = []
    conn.on_connection_lost(lambda: calls.append(1))

    await conn.close()
    conn._on_connection_lost(None)  # the hook close() would drive from the loop

    assert calls == []


async def test_transport_hook_clears_the_client_and_unit_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A transport drop downs the connection: the dead client (and the unit-bound
    # clients cached against it) are cleared, so the next request reconnects
    # against a fresh client.
    fake = _FakeFileClient()
    unit = _unit_over(monkeypatch, fake)
    conn = unit._conn
    await unit.read_file_record(file=4, record=1, length=2)  # connect + cache

    conn._on_connection_lost(TModbusConnectionError("link down"))

    assert conn.connected is False
    assert conn._unit_clients == {}
