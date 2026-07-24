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


def test_tmodbus_rejects_udp_params() -> None:
    # tmodbus has no UDP transport; the error points at the client that does.
    with pytest.raises(TypeError, match="pymodbus.ModbusConnection"):
        tmodbus_backend.ModbusConnection(ModbusUdpParams(host="127.0.0.1"))


def test_tmodbus_rejects_ascii_over_tcp() -> None:
    # tmodbus has no ASCII-over-TCP transport; the error points at the client
    # that does.
    with pytest.raises(ValueError, match="pymodbus.ModbusConnection"):
        tmodbus_backend.ModbusConnection(
            ModbusTcpParams(host="127.0.0.1", framer="ascii")
        )


@pytest.mark.parametrize(
    "params",
    [
        pytest.param(ModbusTcpParams(host="127.0.0.1", framer="ascii"), id="tcp-ascii"),
        pytest.param(ModbusUdpParams(host="127.0.0.1"), id="udp"),
        pytest.param(
            ModbusSerialParams(device="/dev/null", framer="ascii"), id="serial-ascii"
        ),
    ],
)
def test_pymodbus_accepts_the_full_matrix(params: object) -> None:
    # Every params type — including the two tmodbus rejects — constructs
    # without I/O on the pymodbus client.
    assert pymodbus_backend.ModbusConnection(params).connected is False  # type: ignore[arg-type]
