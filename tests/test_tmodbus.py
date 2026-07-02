"""tmodbus-backend unit tests that don't need a live server.

These exercise the seams the shared end-to-end suite can't: file records (the
pymodbus test server ships a broken dummy file-record handler, so the wrapper's
word<->byte conversion is verified here against a fake client) and the reactive
connection-lost detection.
"""

from __future__ import annotations

import pytest
from tmodbus.exceptions import InvalidResponseError
from tmodbus.exceptions import ModbusConnectionError as TModbusConnectionError

from modbus_connection import ModbusConnectionError, ModbusTimeoutError
from modbus_connection.tmodbus import TmodbusConnection, TmodbusUnit


class _FakeFileClient:
    """A stand-in tmodbus client that records file-record calls.

    tmodbus 0.4.0 exposes ``read_file_record`` / ``write_file_record`` directly
    (no raw ``execute(pdu)`` seam): the read returns the record's raw data bytes
    and the write takes the payload bytes. This captures the arguments and hands
    back a canned read payload so the wrapper's word<->byte conversion is tested.
    """

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


async def test_read_file_record_decodes_to_words() -> None:
    client = _FakeFileClient(b"\x00\x2a\x01\x00")  # words 42 and 256
    unit = TmodbusUnit(object(), client)  # type: ignore[arg-type]

    words = await unit.read_file_record(file=4, record=1, length=2)

    assert words == [42, 256]
    assert client.read_calls == [(4, 1, 2)]


async def test_write_file_record_encodes_words_to_payload() -> None:
    client = _FakeFileClient()
    unit = TmodbusUnit(object(), client)  # type: ignore[arg-type]

    await unit.write_file_record(file=7, record=9, values=[42, 256])

    assert client.write_calls == [(7, 9, b"\x00\x2a\x01\x00")]


class _InvalidResponseClient:
    """A unit client whose reads always fail with an invalid response."""

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        raise InvalidResponseError("bad CRC", response_bytes=b"\x00")


async def test_invalid_response_maps_to_timeout() -> None:
    # tmodbus 0.4.0 raises InvalidResponseError for any garbled/unparseable reply;
    # like the pymodbus backend's ModbusIOException, it surfaces as a timeout since
    # no valid response arrived.
    unit = TmodbusUnit(TmodbusConnection(object()), _InvalidResponseClient())  # type: ignore[arg-type]

    with pytest.raises(ModbusTimeoutError):
        await unit.read_holding_registers(0, 1)


class _DroppingClient:
    """A unit client whose reads always fail as a lost connection."""

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        raise TModbusConnectionError("link down")


async def test_on_connection_lost_fires_once_across_repeated_failures() -> None:
    conn = TmodbusConnection(object())  # type: ignore[arg-type]
    calls: list[int] = []
    conn.on_connection_lost(lambda: calls.append(1))
    unit = TmodbusUnit(conn, _DroppingClient())  # type: ignore[arg-type]

    for _ in range(3):
        with pytest.raises(ModbusConnectionError):
            await unit.read_holding_registers(0, 1)

    assert calls == [1]  # detected reactively, fired once despite three failures
