"""Each backend validates its supported params combinations before any I/O."""

from __future__ import annotations

import pytest

import modbus_connection.pymodbus as pymodbus_backend
import modbus_connection.tmodbus as tmodbus_backend
from modbus_connection import (
    ModbusSerialParams,
    ModbusTcpParams,
    ModbusUdpParams,
)


@pytest.mark.parametrize("framer", ["rtu", "ascii"])
def test_tmodbus_rejects_non_socket_udp_framing(framer: str) -> None:
    # tmodbus' UDP transport speaks MBAP only; the error points at the client
    # that carries the other framings.
    with pytest.raises(ValueError, match="pymodbus.ModbusConnection"):
        tmodbus_backend.ModbusConnection(
            ModbusUdpParams(host="127.0.0.1", framer=framer)  # type: ignore[arg-type]
        )


def test_tmodbus_accepts_socket_udp_framing() -> None:
    # The default (MBAP) framing constructs without I/O.
    conn = tmodbus_backend.ModbusConnection(ModbusUdpParams(host="127.0.0.1"))
    assert conn.connected is False


def test_tmodbus_accepts_ascii_over_tcp() -> None:
    # serialx's socket:// transport carries the ASCII framing over a socket.
    conn = tmodbus_backend.ModbusConnection(
        ModbusTcpParams(host="127.0.0.1", framer="ascii")
    )
    assert conn.connected is False


@pytest.mark.parametrize(
    "params",
    [
        pytest.param(ModbusTcpParams(host="127.0.0.1", framer="ascii"), id="tcp-ascii"),
        pytest.param(ModbusUdpParams(host="127.0.0.1"), id="udp"),
        pytest.param(ModbusUdpParams(host="127.0.0.1", framer="rtu"), id="udp-rtu"),
        pytest.param(
            ModbusSerialParams(device="/dev/null", framer="ascii"), id="serial-ascii"
        ),
    ],
)
def test_pymodbus_accepts_the_full_matrix(params: object) -> None:
    # Every params type — including the framings tmodbus rejects — constructs
    # without I/O on the pymodbus client.
    assert pymodbus_backend.ModbusConnection(params).connected is False  # type: ignore[arg-type]
