"""Tests for the query-helper building blocks (modbus_connection.cli_helper).

Covers the three pieces a query script imports instead of re-implementing:
argument parsing → connection, the read-counting unit wrapper, and the
reflection-based field printer.
"""

from __future__ import annotations

import argparse
import io
import sys
from enum import IntEnum
from typing import Any

import pytest

import modbus_connection.pymodbus as pymodbus_backend
import modbus_connection.tmodbus as tmodbus_backend
from modbus_connection import (
    ModbusConnectionError,
    ModbusError,
    ModbusSerialParams,
    ModbusTcpParams,
    ModbusTlsParams,
    ModbusUdpParams,
)
from modbus_connection._protocol import ModbusUnit
from modbus_connection.cli_helper import (
    CountingUnit,
    add_connection_args,
    connect_from_args,
    field_rows,
    print_component,
)
from modbus_connection.mock import MockModbusConnection
from modbus_connection.model import Component, coil, enum, gauge, integer


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_connection_args(parser)
    return parser.parse_args(argv)


# -- connect_from_args --------------------------------------------------------


async def test_connect_tcp_from_args(modbus_server: tuple[str, int]) -> None:
    host, port = modbus_server
    args = _parse([host, "--port", str(port)])
    conn = await connect_from_args(args)
    try:
        # The TCP client is lazy: the first read is what connects it.
        assert await conn.for_unit(1).read_holding_registers(0, 1) == [1234]
        assert conn.connected
    finally:
        await conn.close()


async def test_connect_from_args_maps_failure() -> None:
    # Nothing is listening on this port. The lazy client does no I/O at
    # connect_from_args, so the neutral ModbusConnectionError surfaces on the
    # first request instead of a raw backend exception.
    args = _parse(["127.0.0.1", "--port", "1"])
    conn = await connect_from_args(args)
    try:
        with pytest.raises(ModbusConnectionError):
            await conn.for_unit(1).read_holding_registers(0, 1)
    finally:
        await conn.close()


async def test_connect_from_args_dispatches_by_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # tmodbus is preferred where it carries the transport/framing; UDP and
    # ASCII-over-TCP go to the pymodbus client.
    seen: dict[str, Any] = {}

    def _fake_client(backend: str) -> type:
        class _FakeClient:
            def __init__(
                self, params: Any, *, timeout: float, message_spacing: float
            ) -> None:
                seen["backend"], seen["params"] = backend, params

        return _FakeClient

    monkeypatch.setattr(tmodbus_backend, "ModbusConnection", _fake_client("tmodbus"))
    monkeypatch.setattr(pymodbus_backend, "ModbusConnection", _fake_client("pymodbus"))

    await connect_from_args(_parse(["/dev/ttyUSB0", "--transport", "serial"]))
    assert seen["backend"] == "tmodbus"
    assert isinstance(seen["params"], ModbusSerialParams)
    assert seen["params"].device == "/dev/ttyUSB0"
    assert seen["params"].baudrate == 9600
    assert seen["params"].framer == "rtu"

    await connect_from_args(_parse(["1.2.3.4", "--framer", "rtu", "--port", "1502"]))
    assert seen["backend"] == "tmodbus"
    assert isinstance(seen["params"], ModbusTcpParams)
    assert seen["params"].framer == "rtu"
    assert seen["params"].port == 1502

    await connect_from_args(
        _parse(["dev.local", "--transport", "tls", "--tls-ca", "ca"])
    )
    assert seen["backend"] == "tmodbus"
    assert isinstance(seen["params"], ModbusTlsParams)
    assert seen["params"].verify == "ca"
    assert seen["params"].port == 802

    await connect_from_args(
        _parse(["dev.local", "--transport", "tls", "--tls-no-verify"])
    )
    assert seen["params"].verify is False

    await connect_from_args(_parse(["1.2.3.4", "--transport", "udp"]))
    assert seen["backend"] == "pymodbus"
    assert isinstance(seen["params"], ModbusUdpParams)
    assert seen["params"].port == 502

    await connect_from_args(_parse(["1.2.3.4", "--framer", "ascii"]))
    assert seen["backend"] == "pymodbus"
    assert isinstance(seen["params"], ModbusTcpParams)
    assert seen["params"].framer == "ascii"


def test_unset_port_and_framer_left_to_backend() -> None:
    # Left unset so the backend default applies rather than being forced here.
    args = _parse(["dev.local"])
    assert args.port is None
    assert args.framer is None


# -- backend detection --------------------------------------------------------


async def test_connect_from_args_uses_pymodbus_when_tmodbus_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A None entry makes ``import modbus_connection.tmodbus`` raise ImportError,
    # standing in for the tmodbus dependency not being installed.
    monkeypatch.setitem(sys.modules, "modbus_connection.tmodbus", None)
    captured: dict[str, Any] = {}

    class _FakeClient:
        def __init__(
            self, params: Any, *, timeout: float, message_spacing: float
        ) -> None:
            captured["params"] = params

    monkeypatch.setattr(pymodbus_backend, "ModbusConnection", _FakeClient)
    assert isinstance(await connect_from_args(_parse(["host"])), _FakeClient)
    assert captured["params"] == ModbusTcpParams(host="host", port=502)


async def test_connect_from_args_errors_without_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "modbus_connection.tmodbus", None)
    monkeypatch.setitem(sys.modules, "modbus_connection.pymodbus", None)
    with pytest.raises(ModbusError, match="no Modbus backend installed"):
        await connect_from_args(_parse(["host"]))


# -- narrowing transports / framers -------------------------------------------


def test_single_transport_drops_the_flag() -> None:
    parser = argparse.ArgumentParser()
    add_connection_args(parser, connections=(("serial", "rtu"), ("serial", "ascii")))
    args = parser.parse_args(["/dev/ttyUSB0"])
    assert args.transport == "serial"  # fixed, no flag needed
    with pytest.raises(SystemExit):
        parser.parse_args(["/dev/ttyUSB0", "--transport", "tcp"])


def test_serial_only_omits_network_and_tls_args() -> None:
    parser = argparse.ArgumentParser()
    add_connection_args(parser, connections=(("serial", "rtu"), ("serial", "ascii")))
    args = parser.parse_args(["/dev/ttyUSB0", "--baudrate", "19200"])
    assert args.baudrate == 19200
    assert not hasattr(args, "port")
    assert not hasattr(args, "tls_ca")


def test_tcp_only_omits_serial_and_tls_groups() -> None:
    parser = argparse.ArgumentParser()
    add_connection_args(parser, connections=(("tcp", "socket"), ("tcp", "rtu")))
    args = parser.parse_args(["host"])
    assert hasattr(args, "port")
    assert not hasattr(args, "baudrate")
    assert not hasattr(args, "tls_ca")


def test_single_framer_is_fixed_not_offered() -> None:
    parser = argparse.ArgumentParser()
    add_connection_args(parser, connections=(("tcp", "rtu"),))
    assert parser.parse_args(["host"]).framer == "rtu"  # fixed default
    with pytest.raises(SystemExit):
        parser.parse_args(["host", "--framer", "socket"])


def test_restricted_framers_limits_choices() -> None:
    parser = argparse.ArgumentParser()
    add_connection_args(parser, connections=(("tcp", "socket"), ("tcp", "rtu")))
    assert parser.parse_args(["host", "--framer", "rtu"]).framer == "rtu"
    with pytest.raises(SystemExit):
        parser.parse_args(["host", "--framer", "ascii"])  # dropped from choices


async def test_fixed_framer_is_passed_to_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _FakeClient:
        def __init__(self, params: Any, **kwargs: Any) -> None:
            captured["params"] = params

    monkeypatch.setattr(tmodbus_backend, "ModbusConnection", _FakeClient)
    parser = argparse.ArgumentParser()
    add_connection_args(parser, connections=(("tcp", "rtu"),))
    await connect_from_args(parser.parse_args(["host"]))
    assert captured["params"].framer == "rtu"


async def test_message_spacing_is_passed_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # message_spacing is a device property the tool sets, not a CLI argument.
    captured: dict[str, Any] = {}

    class _FakeClient:
        def __init__(
            self, params: Any, *, timeout: float, message_spacing: float
        ) -> None:
            captured["message_spacing"] = message_spacing

    monkeypatch.setattr(tmodbus_backend, "ModbusConnection", _FakeClient)
    await connect_from_args(_parse(["host"]), message_spacing=0.05)
    assert captured["message_spacing"] == 0.05


def test_invalid_connections_raise() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        add_connection_args(argparse.ArgumentParser(), connections=())
    with pytest.raises(ValueError, match="unknown transport"):
        add_connection_args(argparse.ArgumentParser(), connections=(("bogus", None),))
    with pytest.raises(ValueError, match="not valid for transport"):
        add_connection_args(
            argparse.ArgumentParser(), connections=(("serial", "socket"),)
        )
    with pytest.raises(ValueError, match="not valid for transport"):
        add_connection_args(argparse.ArgumentParser(), connections=(("tls", "rtu"),))


# -- CountingUnit -------------------------------------------------------------


def test_counting_unit_delegates_every_protocol_member() -> None:
    # A CountingUnit must forward the whole ModbusUnit surface — every method it
    # doesn't define itself silently disappears from the wrapped unit. Enumerate
    # the protocol so a newly added member (set_message_spacing was once missed)
    # fails here until it gets a matching delegate.
    members = {name for name in dir(ModbusUnit) if not name.startswith("_")}
    missing = members - set(dir(CountingUnit))
    assert not missing, f"CountingUnit is missing delegates for: {sorted(missing)}"


async def test_counting_unit_counts_reads_and_delegates() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 11, 1: 22})
    unit.coils.update({0: True})

    counting = CountingUnit(unit)  # no cast: CountingUnit is a ModbusUnit
    assert await counting.read_holding_registers(0, 2) == [11, 22]
    assert await counting.read_coils(0, 1) == [True]
    assert counting.reads == 2

    # Non-read methods and attributes delegate untouched (not counted).
    assert counting.connected is unit.connected
    await counting.write_register(0, 99)
    assert unit.holding[0] == 99
    assert counting.reads == 2


class _State(IntEnum):
    IDLE = 0
    RUNNING = 1


class _Meter(Component):
    temperature = gauge(0, 0.1, unit="°C")
    count = integer(1, signed=False)
    state = enum(2, _State)
    relay = coil(0)

    @property
    def label(self) -> str | None:
        return None if self.count is None else f"meter-{self.count}"


async def test_counting_unit_tallies_a_component_update() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 235, 1: 7, 2: 1})
    unit.coils.update({0: True})

    counting = CountingUnit(unit)
    meter = _Meter(counting)  # CountingUnit drops in wherever a ModbusUnit goes
    await meter.async_update()

    # Contiguous holding registers pool into one read; coils are a second space.
    assert counting.reads == 2
    assert meter.temperature == pytest.approx(23.5)


# -- field reflection ---------------------------------------------------------


def _read_meter() -> _Meter:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 235, 1: 7, 2: 1})
    unit.coils.update({0: True})
    return _Meter(unit)


async def test_field_rows_reflects_fields_units_and_properties() -> None:
    meter = _read_meter()
    await meter.async_update()
    rows = dict(field_rows(meter))

    assert rows["temperature"] == "23.5 °C"  # scaled value carries its unit
    assert rows["count"] == "7"  # no unit → bare value
    assert rows["state"] == "running"  # IntEnum rendered by name, lowercased
    assert rows["relay"] == "True"  # coil bool
    assert rows["label"] == "meter-7"  # computed @property included

    # Internals and methods are not fields.
    assert "async_update" not in rows
    assert "register_items" not in rows


def test_field_rows_unread_renders_placeholder() -> None:
    rows = dict(field_rows(_read_meter()))  # never updated
    assert rows["temperature"] == "— °C"  # placeholder still carries the unit
    assert rows["label"] == "—"  # property over an unread field


async def test_print_component_writes_aligned_block() -> None:
    meter = _read_meter()
    await meter.async_update()
    buffer = io.StringIO()
    print_component(meter, title="Sensors", file=buffer)
    text = buffer.getvalue()

    lines = text.splitlines()
    assert lines[0] == "Sensors"
    assert lines[1] == "-------"
    assert "  temperature  23.5 °C" in lines
    # Names are left-padded to a common width, so values line up.
    starts = {line.index(line.split()[1]) for line in lines[2:] if line.split()}
    assert len(starts) == 1


async def test_print_component_defaults_title_to_class_name() -> None:
    meter = _read_meter()
    await meter.async_update()
    buffer = io.StringIO()
    print_component(meter, file=buffer)
    assert buffer.getvalue().splitlines()[0] == "_Meter"
