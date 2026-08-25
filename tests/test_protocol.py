"""Protocol conformance: the backends and the mock satisfy the public types."""

from __future__ import annotations

from modbus_connection import ModbusConnection, ModbusUnit
from modbus_connection.mock import MockModbusConnection
from modbus_connection.pymodbus import connect_tcp as pymodbus_connect_tcp
from modbus_connection.tmodbus import connect_tcp as tmodbus_connect_tcp

from .conftest import UNIT_ID

# ModbusConnection is the abstract connection base class — the backends and
# the mock all subclass it — while ModbusUnit is a runtime_checkable Protocol
# checked structurally.


def _annotated(conn: ModbusConnection, unit: ModbusUnit) -> None:
    """Exists to be type-checked: the mock fits the public annotations."""


def test_mock_satisfies_the_annotations_statically() -> None:
    # The body is the assertion — mypy fails this file if the mock stops
    # fitting the Protocols (issue #121's cast-at-every-seam problem).
    mock = MockModbusConnection()
    _annotated(mock, mock.for_unit(1))


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


def test_mock_satisfies_connection_type() -> None:
    # The in-memory mock stands in for a real connection in consumer tests, so
    # isinstance checks against ModbusConnection must keep passing for it.
    mock = MockModbusConnection()
    assert isinstance(mock, ModbusConnection)
    assert isinstance(mock.for_unit(UNIT_ID), ModbusUnit)
