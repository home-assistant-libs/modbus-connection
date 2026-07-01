"""tmodbus-backend unit tests that don't need a live server.

These exercise the seams the shared end-to-end suite can't: file records go
through tmodbus's ``execute(pdu)`` path (and pymodbus's test server ships a
broken dummy file-record handler, so the return-shape is verified here against
tmodbus's own PDU decoding instead), and the reactive connection-lost detection.
"""

from __future__ import annotations

from typing import Any

import pytest
from tmodbus.exceptions import ModbusConnectionError as TModbusConnectionError
from tmodbus.pdu import ReadFileRecordPDU

from modbus_connection import ModbusConnectionError
from modbus_connection.tmodbus import TmodbusConnection, TmodbusUnit


class _FakeClient:
    """A stand-in tmodbus client whose ``execute`` decodes a canned frame.

    tmodbus's ``AsyncModbusClient.execute(pdu)`` sends the request and returns
    ``pdu.decode_response(raw)``; this mirrors that so the wrapper is tested
    against the real ``ReadFileRecordPDU`` decoder rather than a hand-rolled shape.
    """

    def __init__(self, response: bytes) -> None:
        self._response = response
        self.executed: list[Any] = []

    async def execute(self, pdu: Any) -> Any:
        self.executed.append(pdu)
        return pdu.decode_response(self._response)


async def test_read_file_record_decodes_to_words() -> None:
    # FC20 response: fc, byte_count, then [record_length, ref_type=6, data...].
    # record_length counts the reference-type byte, so 4 data bytes -> length 5.
    data = b"\x00\x2a\x01\x00"  # words 42 and 256
    response = bytes([0x14, len(data) + 2, len(data) + 1, 0x06]) + data
    unit = TmodbusUnit(object(), _FakeClient(response))  # type: ignore[arg-type]

    words = await unit.read_file_record(file=4, record=1, length=2)

    assert words == [42, 256]


async def test_read_file_record_builds_expected_request() -> None:
    response = bytes([0x14, 0x03, 0x02, 0x06, 0x00])  # one empty-ish record
    client = _FakeClient(response)
    unit = TmodbusUnit(object(), client)  # type: ignore[arg-type]

    await unit.read_file_record(file=7, record=9, length=3)

    (pdu,) = client.executed
    assert isinstance(pdu, ReadFileRecordPDU)
    (request,) = pdu.requests
    assert (request.file_number, request.record_number, request.record_length) == (
        7,
        9,
        3,
    )


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
