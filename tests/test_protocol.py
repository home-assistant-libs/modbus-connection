"""Protocol conformance and backend-specific NotImplementedError behavior."""

from __future__ import annotations

from typing import Any

import pytest

from modbus_connection import ModbusConnection, ModbusUnit
from modbus_connection.mock import MockModbusConnection
from modbus_connection.pymodbus import connect_tcp as pymodbus_connect_tcp
from modbus_connection.tmodbus import connect_tcp as tmodbus_connect_tcp

from .conftest import UNIT_ID

# ModbusConnection is the abstract connection base class (both backend
# connections subclass it; the mock is a registered virtual subclass), while
# ModbusUnit is a runtime_checkable Protocol checked on live instances.


async def test_pymodbus_instances_satisfy_protocols(
    modbus_server: tuple[str, int],
) -> None:
    host, port = modbus_server
    conn = await pymodbus_connect_tcp(host, port=port)
    try:
        assert isinstance(conn, ModbusConnection)
        assert isinstance(conn.for_unit(UNIT_ID), ModbusUnit)
    finally:
        await conn.close()


async def test_tmodbus_instances_satisfy_protocols(
    modbus_server: tuple[str, int],
) -> None:
    host, port = modbus_server
    conn = await tmodbus_connect_tcp(host, port=port)
    try:
        assert isinstance(conn, ModbusConnection)
        assert isinstance(conn.for_unit(UNIT_ID), ModbusUnit)
    finally:
        await conn.close()


@pytest.mark.parametrize(
    "method",
    ["diagnostics", "get_comm_event_counter", "get_comm_event_log"],
)
async def test_tmodbus_unsupported_codes_raise(
    modbus_server: tuple[str, int], method: str
) -> None:
    host, port = modbus_server
    conn = await tmodbus_connect_tcp(host, port=port)
    try:
        unit: Any = conn.for_unit(UNIT_ID)
        with pytest.raises(NotImplementedError):
            if method == "diagnostics":
                await unit.diagnostics(0, 0)
            else:
                await getattr(unit, method)()
    finally:
        await conn.close()


def test_mock_satisfies_connection_type() -> None:
    # The in-memory mock stands in for a real connection in consumer tests, so
    # isinstance checks against ModbusConnection must keep passing for it.
    mock = MockModbusConnection()
    assert isinstance(mock, ModbusConnection)
    assert isinstance(mock.for_unit(UNIT_ID), ModbusUnit)
