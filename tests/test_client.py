"""The shared connection-params dataclasses: frozen, keyword-only, hashable.

Equal instances hash equal, so a params object doubles as a connection
identity key.
"""

from __future__ import annotations

import dataclasses

import pytest

from modbus_connection import (
    ModbusSerialParams,
    ModbusTcpParams,
    ModbusTlsParams,
    ModbusUdpParams,
)


def test_tcp_params_defaults_and_frozen() -> None:
    params = ModbusTcpParams(host="dev.local")
    assert (params.port, params.framer) == (502, "socket")
    with pytest.raises(dataclasses.FrozenInstanceError):
        params.host = "other"  # type: ignore[misc]


def test_serial_params_defaults_and_frozen() -> None:
    params = ModbusSerialParams(device="/dev/ttyUSB0")
    assert (
        params.baudrate,
        params.bytesize,
        params.parity,
        params.stopbits,
        params.framer,
    ) == (9600, 8, "N", 1, "rtu")
    with pytest.raises(dataclasses.FrozenInstanceError):
        params.baudrate = 19200  # type: ignore[misc]


def test_udp_params_defaults_and_frozen() -> None:
    params = ModbusUdpParams(host="dev.local")
    assert (params.port, params.framer) == (502, "socket")
    with pytest.raises(dataclasses.FrozenInstanceError):
        params.host = "other"  # type: ignore[misc]


def test_tls_params_defaults_and_frozen() -> None:
    params = ModbusTlsParams(host="dev.local")
    assert (params.port, params.verify, params.check_hostname) == (802, True, True)
    assert (params.client_cert, params.client_key, params.client_key_password) == (
        None,
        None,
        None,
    )
    assert params.sslctx is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        params.verify = False  # type: ignore[misc]


@pytest.mark.parametrize(
    ("params_cls", "kwargs"),
    [
        pytest.param(ModbusTcpParams, {"host": "dev.local"}, id="tcp"),
        pytest.param(ModbusUdpParams, {"host": "dev.local"}, id="udp"),
        pytest.param(ModbusTlsParams, {"host": "dev.local"}, id="tls"),
        pytest.param(ModbusSerialParams, {"device": "/dev/ttyUSB0"}, id="serial"),
    ],
)
def test_params_are_keyword_only_and_hashable(
    params_cls: type, kwargs: dict[str, str]
) -> None:
    with pytest.raises(TypeError):
        params_cls(*kwargs.values())
    assert {params_cls(**kwargs), params_cls(**kwargs)} == {params_cls(**kwargs)}
