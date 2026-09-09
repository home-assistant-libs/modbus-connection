"""Tests for the timing a device library requires through its unit.

A device library is the only layer that knows the device, and it only ever
holds a ``ModbusUnit``. ``require_timeout`` and ``require_connect_delay`` let it
say what the device needs from there. Both are floors: the connection runs with
the largest value asked of it, by the connection itself or by any unit on it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from modbus_connection import ModbusTcpParams, ModbusUnit
from modbus_connection._client import BaseModbusConnection
from modbus_connection.mock import MockModbusConnection
from modbus_connection.pymodbus import ModbusConnection as PymodbusConnection
from modbus_connection.tmodbus import ModbusConnection as TmodbusConnection

UNIT_A = 1
UNIT_B = 2


class _FakeConnection(BaseModbusConnection):
    """Connects to nothing, so requirements can be driven without a server."""

    def __init__(self, **tuning: float) -> None:
        super().__init__(ModbusTcpParams(host="host"), **tuning)
        self.closed_clients = 0

    async def _connect_client(self) -> Any:
        return object()

    async def _close_client(self, client: Any) -> None:
        self.closed_clients += 1

    def for_unit(self, unit_id: int) -> ModbusUnit:
        raise NotImplementedError


# -- resolving the requirements -----------------------------------------------


def test_a_requirement_raises_the_connections_own_value() -> None:
    conn = _FakeConnection(timeout=3)

    conn._require_timeout(UNIT_A, 30)

    assert conn._timeout == 30


def test_the_connections_own_value_wins_when_it_is_larger() -> None:
    """A requirement is a floor, so it never shortens what the caller asked for."""
    conn = _FakeConnection(timeout=30)

    conn._require_timeout(UNIT_A, 3)

    assert conn._timeout == 30


def test_the_most_demanding_unit_wins() -> None:
    conn = _FakeConnection(timeout=3)

    conn._require_timeout(UNIT_A, 10)
    conn._require_timeout(UNIT_B, 30)

    assert conn._timeout == 30


def test_zero_clears_a_requirement() -> None:
    conn = _FakeConnection(timeout=3)
    conn._require_timeout(UNIT_A, 30)

    conn._require_timeout(UNIT_A, 0)

    assert conn._timeout == 3


def test_connect_delay_resolves_the_same_way() -> None:
    conn = _FakeConnection(connect_delay=0.5)

    conn._require_connect_delay(UNIT_A, 2)

    assert conn._connect_delay == 2


@pytest.mark.parametrize(
    "require", ["_require_timeout", "_require_connect_delay"], ids=["timeout", "delay"]
)
def test_a_negative_requirement_is_rejected(require: str) -> None:
    conn = _FakeConnection()

    with pytest.raises(ValueError):
        getattr(conn, require)(UNIT_A, -1)


# -- reaching a link that is already up ---------------------------------------


async def test_a_requirement_set_before_connecting_needs_no_recycle() -> None:
    conn = _FakeConnection(timeout=3)
    conn._require_timeout(UNIT_A, 30)

    await conn.connect()

    assert conn.connected is True
    assert conn._timeout == 30
    assert conn.closed_clients == 0


async def test_raising_the_timeout_recycles_a_live_link() -> None:
    """The client is built with the timeout, so the unit would keep giving up early."""
    conn = _FakeConnection(timeout=3)
    await conn.connect()

    conn._require_timeout(UNIT_A, 30)
    await asyncio.sleep(0.01)  # the recycle is scheduled, not awaited

    assert conn.connected is False
    assert conn.closed_clients == 1


async def test_relaxing_the_timeout_leaves_a_live_link_alone() -> None:
    """Nothing is served worse by a patient client, so it can wait for a reconnect."""
    conn = _FakeConnection(timeout=3)
    conn._require_timeout(UNIT_A, 30)
    await conn.connect()

    conn._require_timeout(UNIT_A, 0)
    await asyncio.sleep(0.01)

    assert conn.connected is True
    assert conn.closed_clients == 0
    assert conn._timeout == 3


async def test_a_connect_delay_requirement_leaves_a_live_link_alone() -> None:
    """It is awaited on the next connect, so the link in place is not disturbed."""
    conn = _FakeConnection()
    await conn.connect()

    conn._require_connect_delay(UNIT_A, 1)
    await asyncio.sleep(0.01)

    assert conn.connected is True
    assert conn._connect_delay == 1


# -- the unit surface every implementation carries ----------------------------


@pytest.mark.parametrize(
    "connection_class",
    [PymodbusConnection, TmodbusConnection],
    ids=["pymodbus", "tmodbus"],
)
def test_a_units_requirement_reaches_its_connection(
    connection_class: type[BaseModbusConnection],
) -> None:
    conn = connection_class(ModbusTcpParams(host="host"), timeout=3)

    conn.for_unit(UNIT_A).require_timeout(30)
    conn.for_unit(UNIT_A).require_connect_delay(1)

    assert conn._timeout == 30
    assert conn._connect_delay == 1


def test_the_mock_records_what_a_library_requires() -> None:
    """Tests over the mock assert on the timing a device library asked for."""
    unit = MockModbusConnection().for_unit(UNIT_A)

    unit.require_timeout(30)
    unit.require_connect_delay(1)

    assert unit.required_timeout == 30
    assert unit.required_connect_delay == 1
