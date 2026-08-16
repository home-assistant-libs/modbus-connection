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

Params = ModbusTcpParams | ModbusUdpParams | ModbusTlsParams | ModbusSerialParams


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


@pytest.mark.parametrize(
    ("params_cls", "kwargs", "message"),
    [
        pytest.param(
            ModbusTcpParams,
            {"host": "dev.local", "framer": "bogus"},
            "unknown framer 'bogus'; expected 'socket', 'rtu', or 'ascii'",
            id="tcp",
        ),
        pytest.param(
            ModbusUdpParams,
            {"host": "dev.local", "framer": "bogus"},
            "unknown framer 'bogus'; expected 'socket', 'rtu', or 'ascii'",
            id="udp",
        ),
        pytest.param(
            ModbusSerialParams,
            {"device": "/dev/ttyUSB0", "framer": "socket"},
            "unknown serial framer 'socket'; expected 'rtu' or 'ascii'",
            id="serial",
        ),
    ],
)
def test_params_reject_unknown_framer(
    params_cls: type, kwargs: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=f"^{message}$"):
        params_cls(**kwargs)


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


@pytest.mark.parametrize(
    ("left", "right"),
    [
        pytest.param(
            ModbusTcpParams(host="dev.local", framer="socket"),
            ModbusTcpParams(host="dev.local", framer="rtu"),
            id="tcp-framer-ignored",
        ),
        pytest.param(
            ModbusTcpParams(host="Dev.LOCAL"),
            ModbusTcpParams(host="dev.local"),
            id="tcp-host-case-insensitive",
        ),
        pytest.param(
            ModbusUdpParams(host="fe80::1"),
            ModbusUdpParams(host="FE80::1"),
            id="udp-ipv6-case-insensitive",
        ),
        pytest.param(
            ModbusTlsParams(host="dev.local", port=802, verify=False),
            ModbusTlsParams(host="dev.local", port=802, verify=True),
            id="tls-settings-ignored",
        ),
        pytest.param(
            ModbusTcpParams(host="dev.local", port=802),
            ModbusTlsParams(host="dev.local", port=802),
            id="tcp-and-tls-share-endpoint",
        ),
        pytest.param(
            ModbusSerialParams(device="/dev/ttyUSB0", baudrate=9600),
            ModbusSerialParams(device="/dev/ttyUSB0", baudrate=19200, parity="E"),
            id="serial-line-settings-ignored",
        ),
    ],
)
def test_endpoint_same_device(left: Params, right: Params) -> None:
    assert left.endpoint == right.endpoint
    assert hash(left.endpoint) == hash(right.endpoint)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        pytest.param(
            ModbusTcpParams(host="Dev.LOCAL"),
            ModbusTcpParams(host="dev.local"),
            id="tcp",
        ),
        pytest.param(
            ModbusUdpParams(host="FE80::1"),
            ModbusUdpParams(host="fe80::1"),
            id="udp",
        ),
        pytest.param(
            ModbusTlsParams(host="Dev.LOCAL"),
            ModbusTlsParams(host="dev.local"),
            id="tls",
        ),
    ],
)
def test_host_case_does_not_make_params_differ(left: Params, right: Params) -> None:
    """The host is folded on construction, so equality and hashing agree."""
    assert left == right
    assert hash(left) == hash(right)
    assert left.host == right.host


@pytest.mark.parametrize(
    ("left", "right"),
    [
        pytest.param(
            ModbusTcpParams(host="dev.local"),
            ModbusTcpParams(host="other.local"),
            id="tcp-different-host",
        ),
        pytest.param(
            ModbusTcpParams(host="dev.local", port=502),
            ModbusTcpParams(host="dev.local", port=503),
            id="tcp-different-port",
        ),
        pytest.param(
            ModbusTcpParams(host="dev.local", port=502),
            ModbusUdpParams(host="dev.local", port=502),
            id="tcp-vs-udp",
        ),
        pytest.param(
            ModbusSerialParams(device="/dev/ttyUSB0"),
            ModbusSerialParams(device="/dev/ttyUSB1"),
            id="serial-different-device",
        ),
        pytest.param(
            ModbusSerialParams(device="/dev/ttyUSB0"),
            ModbusTcpParams(host="/dev/ttyUSB0", port=502),
            id="serial-vs-tcp",
        ),
    ],
)
def test_endpoint_different_device(left: Params, right: Params) -> None:
    assert left.endpoint != right.endpoint


def test_endpoint_usable_as_grouping_key() -> None:
    by_endpoint = {
        ModbusSerialParams(device="/dev/ttyUSB0", baudrate=9600).endpoint: "first"
    }
    other = ModbusSerialParams(device="/dev/ttyUSB0", baudrate=19200)
    assert by_endpoint[other.endpoint] == "first"
