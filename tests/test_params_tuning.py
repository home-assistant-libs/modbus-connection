"""Tests for the tuning the params dataclasses carry.

``timeout``, ``message_spacing`` and ``connect_delay`` say how a caller wants
the link run rather than how to open it, so two callers of one device may ask
for different values and still share a connection. These tests cover the three
pieces that makes possible: the transport default for spacing,
``is_compatible_with`` + ``resolve_params``, and ``set_params`` on a live
connection.
"""

from __future__ import annotations

from typing import Any

import pytest

from modbus_connection import (
    ModbusSerialParams,
    ModbusTcpParams,
    ModbusUdpParams,
    ModbusUnit,
    resolve_params,
)
from modbus_connection._client import BaseModbusConnection

SERIAL_DEFAULT_SPACING = 0.03


class _FakeConnection(BaseModbusConnection):
    """Connects to nothing, so tuning can be inspected without a server."""

    async def _connect_client(self) -> Any:
        return object()

    async def _close_client(self, client: Any) -> None:
        pass

    def for_unit(self, unit_id: int) -> ModbusUnit:
        raise NotImplementedError


# -- the transport default for message spacing --------------------------------


def test_serial_defaults_to_the_rs485_turnaround_gap() -> None:
    """A serial link paces itself without being asked: the adapter needs it."""
    params = ModbusSerialParams(device="/dev/ttyUSB0")
    assert params.effective_message_spacing == SERIAL_DEFAULT_SPACING


def test_serial_spacing_can_be_disabled() -> None:
    """An explicit 0 means none, and must not be read as "unset"."""
    params = ModbusSerialParams(device="/dev/ttyUSB0", message_spacing=0)
    assert params.effective_message_spacing == 0


def test_serial_spacing_can_be_widened() -> None:
    params = ModbusSerialParams(device="/dev/ttyUSB0", message_spacing=0.1)
    assert params.effective_message_spacing == 0.1


@pytest.mark.parametrize(
    "params",
    [ModbusTcpParams(host="host"), ModbusUdpParams(host="host")],
    ids=["tcp", "udp"],
)
def test_socket_transports_default_to_no_spacing(params: ModbusTcpParams) -> None:
    assert params.effective_message_spacing == 0


def test_the_connection_applies_the_transport_default() -> None:
    conn = _FakeConnection(ModbusSerialParams(device="/dev/ttyUSB0"))
    assert conn._pacer._message_spacing == SERIAL_DEFAULT_SPACING


# -- validation ---------------------------------------------------------------


@pytest.mark.parametrize(
    "tuning",
    [{"timeout": 0}, {"timeout": -1}, {"connect_delay": -0.1}],
    ids=["zero-timeout", "negative-timeout", "negative-connect-delay"],
)
def test_params_reject_impossible_tuning(tuning: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        ModbusTcpParams(host="host", **tuning)


# -- compatibility ------------------------------------------------------------


def test_tuning_does_not_make_params_incompatible() -> None:
    """The whole point: differing tuning still describes one shared link."""
    first = ModbusTcpParams(host="host", timeout=3)
    second = ModbusTcpParams(host="host", timeout=30, connect_delay=1)
    assert first.is_compatible_with(second)


def test_link_settings_do_make_params_incompatible() -> None:
    """One serial line cannot run at two baud rates."""
    first = ModbusSerialParams(device="/dev/ttyUSB0", baudrate=9600)
    second = ModbusSerialParams(device="/dev/ttyUSB0", baudrate=19200)
    assert not first.is_compatible_with(second)


def test_different_transports_are_incompatible() -> None:
    tcp = ModbusTcpParams(host="host")
    udp = ModbusUdpParams(host="host")
    assert not tcp.is_compatible_with(udp)


# -- resolving between holders ------------------------------------------------


def test_resolve_takes_the_most_demanding_of_each() -> None:
    resolved = resolve_params(
        [
            ModbusTcpParams(host="host", timeout=3, message_spacing=0.1),
            ModbusTcpParams(host="host", timeout=30, connect_delay=1),
        ]
    )
    assert resolved.timeout == 30
    assert resolved.effective_message_spacing == 0.1
    assert resolved.connect_delay == 1


def test_resolve_keeps_the_link_settings() -> None:
    resolved = resolve_params(
        [
            ModbusSerialParams(device="/dev/ttyUSB0", baudrate=19200, timeout=3),
            ModbusSerialParams(device="/dev/ttyUSB0", baudrate=19200, timeout=5),
        ]
    )
    assert resolved == ModbusSerialParams(
        device="/dev/ttyUSB0",
        baudrate=19200,
        timeout=5,
        message_spacing=SERIAL_DEFAULT_SPACING,
    )


def test_resolve_settles_the_transport_default_against_a_request() -> None:
    """An unset holder still asks for the default, so it cannot be undercut."""
    resolved = resolve_params(
        [
            ModbusSerialParams(device="/dev/ttyUSB0"),
            ModbusSerialParams(device="/dev/ttyUSB0", message_spacing=0),
        ]
    )
    assert resolved.effective_message_spacing == SERIAL_DEFAULT_SPACING


def test_resolve_rejects_incompatible_params() -> None:
    with pytest.raises(ValueError):
        resolve_params(
            [
                ModbusSerialParams(device="/dev/ttyUSB0", baudrate=9600),
                ModbusSerialParams(device="/dev/ttyUSB0", baudrate=19200),
            ]
        )


def test_resolve_rejects_no_params() -> None:
    with pytest.raises(ValueError):
        resolve_params([])


# -- applying a change to a live connection -----------------------------------


async def test_set_params_applies_spacing_and_connect_delay_without_a_recycle() -> None:
    conn = _FakeConnection(ModbusTcpParams(host="host"))
    await conn.connect()

    recycle = conn.set_params(
        ModbusTcpParams(host="host", message_spacing=0.2, connect_delay=1)
    )

    assert recycle is False
    assert conn._pacer._message_spacing == 0.2
    assert conn._connect_delay == 1


async def test_set_params_asks_for_a_recycle_when_the_timeout_changes() -> None:
    """The backend client is built with the timeout, so a live link keeps it."""
    conn = _FakeConnection(ModbusTcpParams(host="host", timeout=3))
    await conn.connect()

    assert conn.set_params(ModbusTcpParams(host="host", timeout=30)) is True


def test_set_params_needs_no_recycle_while_the_link_is_down() -> None:
    """Nothing to replace: the next connect builds a client with the new value."""
    conn = _FakeConnection(ModbusTcpParams(host="host", timeout=3))

    assert conn.set_params(ModbusTcpParams(host="host", timeout=30)) is False
    assert conn._timeout == 30


def test_set_params_rejects_a_different_link() -> None:
    conn = _FakeConnection(ModbusTcpParams(host="host"))

    with pytest.raises(ValueError):
        conn.set_params(ModbusTcpParams(host="elsewhere"))


# -- the constructor overrides ------------------------------------------------


def test_the_constructor_overrides_the_params_tuning() -> None:
    """Callers that keep link settings and tuning apart can still pass both."""
    conn = _FakeConnection(ModbusTcpParams(host="host", timeout=3), timeout=30)

    assert conn._timeout == 30
    assert conn._params.timeout == 30


def test_the_constructor_keeps_what_it_does_not_override() -> None:
    conn = _FakeConnection(
        ModbusTcpParams(host="host", message_spacing=0.1, connect_delay=1), timeout=30
    )

    assert conn._pacer._message_spacing == 0.1
    assert conn._connect_delay == 1


def test_the_constructor_can_disable_the_serial_default() -> None:
    conn = _FakeConnection(ModbusSerialParams(device="/dev/ttyUSB0"), message_spacing=0)

    assert conn._pacer._message_spacing == 0


def test_an_overriding_value_is_validated() -> None:
    with pytest.raises(ValueError):
        _FakeConnection(ModbusTcpParams(host="host"), timeout=0)
