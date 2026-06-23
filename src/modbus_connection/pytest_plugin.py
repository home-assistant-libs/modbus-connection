"""pytest fixtures for the in-memory mock backend.

Registered as a ``pytest11`` entry point, so any project that installs
``modbus-connection`` gets these fixtures with no conftest wiring:

    async def test_reads_setpoint(mock_modbus_unit):
        mock_modbus_unit.holding[40] = 1234
        assert await mock_modbus_unit.read_uint16(40) == 1234

The fixtures hand back the concrete ``Mock...`` types so a test can configure
stores and register ``on_write`` callbacks. The code under test still only sees
the ``ModbusConnection`` / ``ModbusUnit`` Protocols.
"""

from __future__ import annotations

import pytest

from .mock import MockModbusConnection, MockModbusUnit

DEFAULT_UNIT_ID = 1


@pytest.fixture
def mock_modbus_connection() -> MockModbusConnection:
    """A fresh in-memory ``MockModbusConnection``."""
    return MockModbusConnection()


@pytest.fixture
def mock_modbus_unit(mock_modbus_connection: MockModbusConnection) -> MockModbusUnit:
    """The unit-1 handle on ``mock_modbus_connection``, ready to configure."""
    return mock_modbus_connection.for_unit(DEFAULT_UNIT_ID)
