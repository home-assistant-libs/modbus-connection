"""Each backend's ModbusConnection validates its supported (params, framer)
combinations at construction, before any I/O is attempted."""

from __future__ import annotations

import pytest

import modbus_connection.pymodbus as pymodbus_backend
import modbus_connection.tmodbus as tmodbus_backend
from modbus_connection import (
    ModbusSerialParams,
    ModbusTcpParams,
    ModbusUdpParams,
)

both_backends = pytest.mark.parametrize(
    "client_cls",
    [pymodbus_backend.ModbusConnection, tmodbus_backend.ModbusConnection],
    ids=["pymodbus", "tmodbus"],
)


@pytest.mark.parametrize(
    ("client_cls", "match"),
    [
        # pymodbus's FramerType enum does the conversion, so its native error
        # names the unknown framing; tmodbus dispatches framings itself.
        pytest.param(
            pymodbus_backend.ModbusConnection,
            "is not a valid FramerType",
            id="pymodbus",
        ),
        pytest.param(tmodbus_backend.ModbusConnection, "unknown framer", id="tmodbus"),
    ],
)
def test_tcp_rejects_unknown_framer(client_cls: type, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        client_cls(ModbusTcpParams(host="127.0.0.1", framer="bogus"))  # type: ignore[arg-type]


@both_backends
def test_serial_rejects_unknown_framer(client_cls: type) -> None:
    with pytest.raises(ValueError, match="unknown serial framer"):
        client_cls(ModbusSerialParams(device="/dev/null", framer="socket"))  # type: ignore[arg-type]


def test_pymodbus_udp_rejects_unknown_framer() -> None:
    with pytest.raises(ValueError, match="is not a valid FramerType"):
        pymodbus_backend.ModbusConnection(
            ModbusUdpParams(host="127.0.0.1", framer="bogus")  # type: ignore[arg-type]
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
