"""Tests for the query-helper building blocks (modbus_connection.cli_helper).

Covers the three pieces a query script imports instead of re-implementing:
argument parsing → connection, the read-counting unit wrapper, and the
reflection-based field printer.
"""

from __future__ import annotations

import argparse
import inspect
import io
import sys
from enum import IntEnum, IntFlag
from typing import Any

import pytest

import modbus_connection.pymodbus as pymodbus_backend
import modbus_connection.tmodbus as tmodbus_backend
from modbus_connection import ModbusConnectionError, ModbusError
from modbus_connection._protocol import ModbusUnit
from modbus_connection.cli_helper import (
    CountingUnit,
    _load_backend,
    add_connection_args,
    connect_from_args,
    field_rows,
    group_rows,
    print_component,
)
from modbus_connection.mock import MockModbusConnection
from modbus_connection.model import (
    Component,
    ManualComponent,
    coil,
    enum,
    flags,
    gauge,
    integer,
    repeating_group,
)


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
        assert conn.connected
        assert await conn.for_unit(1).read_holding_registers(0, 1) == [1234]
    finally:
        await conn.close()


async def test_connect_from_args_maps_failure() -> None:
    # Nothing is listening on this port: a connect failure must surface as the
    # neutral ModbusConnectionError, not a raw backend exception.
    args = _parse(["127.0.0.1", "--port", "1"])
    with pytest.raises(ModbusConnectionError):
        await connect_from_args(args)


def test_factory_signatures_accept_cli_kwargs() -> None:
    # connect_from_args forwards timeout/message_spacing plus per-transport
    # options to the backends' connect_* factories. A factory missing one of
    # them would raise TypeError at connect time, which the dispatch test
    # below can't see through its monkeypatched stand-ins — so bind the real
    # signatures against everything connect_from_args can pass.
    common = {"timeout": 3.0, "message_spacing": 0.0}
    per_factory: dict[str, dict[str, Any]] = {
        "connect_tcp": {"port": 502, "framer": "rtu"},
        "connect_udp": {"port": 502, "framer": "socket"},
        "connect_tls": {
            "port": 802,
            "verify": True,
            "check_hostname": True,
            "client_cert": None,
            "client_key": None,
            "client_key_password": None,
        },
        "connect_serial": {
            "baudrate": 9600,
            "bytesize": 8,
            "parity": "N",
            "stopbits": 1,
            "framer": "rtu",
        },
    }
    for backend in (tmodbus_backend, pymodbus_backend):
        for name, kwargs in per_factory.items():
            signature = inspect.signature(getattr(backend, name))
            signature.bind("target", **kwargs, **common)  # TypeError on mismatch


async def test_connect_from_args_dispatches_by_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    async def record(name: str, target: str, **kwargs: Any) -> str:
        calls["name"], calls["target"], calls["kwargs"] = name, target, kwargs
        return "conn"

    for backend in (tmodbus_backend, pymodbus_backend):
        for func in ("connect_tcp", "connect_udp", "connect_tls", "connect_serial"):
            monkeypatch.setattr(
                backend,
                func,
                lambda target, _f=func, _b=backend.__name__, **kw: record(
                    f"{_b}.{_f}", target, **kw
                ),
            )

    await connect_from_args(_parse(["/dev/ttyUSB0", "--transport", "serial"]))
    assert calls["name"].endswith("tmodbus.connect_serial")
    assert calls["target"] == "/dev/ttyUSB0"
    assert calls["kwargs"]["baudrate"] == 9600

    await connect_from_args(
        _parse(["dev.local", "--transport", "tls", "--tls-ca", "ca"])
    )
    assert calls["name"].endswith("tmodbus.connect_tls")
    assert calls["kwargs"]["verify"] == "ca"

    await connect_from_args(
        _parse(["dev.local", "--transport", "tls", "--tls-no-verify"])
    )
    assert calls["kwargs"]["verify"] is False

    await connect_from_args(_parse(["1.2.3.4", "--framer", "rtu", "--port", "1502"]))
    assert calls["name"].endswith("tmodbus.connect_tcp")
    assert calls["kwargs"]["framer"] == "rtu"
    assert calls["kwargs"]["port"] == 1502

    await connect_from_args(_parse(["1.2.3.4", "--transport", "udp"]))
    assert calls["name"].endswith("tmodbus.connect_udp")

    await connect_from_args(
        _parse(["1.2.3.4", "--transport", "udp", "--framer", "rtu"])
    )
    assert calls["name"].endswith("pymodbus.connect_udp")

    await connect_from_args(_parse(["1.2.3.4", "--framer", "ascii"]))
    assert calls["name"].endswith("pymodbus.connect_tcp")


def test_unset_port_and_framer_left_to_backend() -> None:
    # Left unset so the backend default applies rather than being forced here.
    args = _parse(["dev.local"])
    assert args.port is None
    assert args.framer is None


# -- backend detection --------------------------------------------------------


def test_load_backend_prefers_tmodbus() -> None:
    # Both backends are installed in dev; tmodbus wins.
    assert _load_backend("tcp", None) is tmodbus_backend


def test_load_backend_falls_back_to_pymodbus(monkeypatch: pytest.MonkeyPatch) -> None:
    # A None entry makes ``import modbus_connection.tmodbus`` raise ImportError,
    # standing in for the tmodbus dependency not being installed.
    monkeypatch.setitem(sys.modules, "modbus_connection.tmodbus", None)
    assert _load_backend("tcp", None) is pymodbus_backend


def test_load_backend_routes_unsupported_tmodbus_requests() -> None:
    assert _load_backend("udp", "rtu") is pymodbus_backend
    assert _load_backend("tcp", "ascii") is pymodbus_backend


def test_load_backend_prefers_tmodbus_for_udp() -> None:
    # tmodbus carries MBAP-framed UDP; only rtu/ascii framing needs pymodbus.
    assert _load_backend("udp", None) is tmodbus_backend
    assert _load_backend("udp", "socket") is tmodbus_backend


def test_load_backend_errors_when_none_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "modbus_connection.tmodbus", None)
    monkeypatch.setitem(sys.modules, "modbus_connection.pymodbus", None)
    with pytest.raises(ModbusError, match="no installed Modbus backend supports"):
        _load_backend("tcp", None)


def test_load_backend_reports_pymodbus_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "modbus_connection.pymodbus", None)
    with pytest.raises(ModbusError, match="udp with rtu framing requires pymodbus"):
        _load_backend("udp", "rtu")
    with pytest.raises(ModbusError, match="tcp with ascii framing requires pymodbus"):
        _load_backend("tcp", "ascii")


async def test_connect_from_args_uses_pymodbus_when_tmodbus_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "modbus_connection.tmodbus", None)
    captured: dict[str, Any] = {}

    async def fake(target: str, **kwargs: Any) -> str:
        captured["target"] = target
        return "conn"

    monkeypatch.setattr(pymodbus_backend, "connect_tcp", lambda t, **k: fake(t, **k))
    assert await connect_from_args(_parse(["host"])) == "conn"
    assert captured["target"] == "host"


async def test_connect_from_args_errors_without_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "modbus_connection.tmodbus", None)
    monkeypatch.setitem(sys.modules, "modbus_connection.pymodbus", None)
    with pytest.raises(ModbusError, match="no installed Modbus backend supports"):
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


async def test_fixed_framer_is_passed_to_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake(target: str, **kwargs: Any) -> str:
        captured.update(kwargs)
        return "conn"

    monkeypatch.setattr(tmodbus_backend, "connect_tcp", lambda t, **k: fake(t, **k))
    parser = argparse.ArgumentParser()
    add_connection_args(parser, connections=(("tcp", "rtu"),))
    await connect_from_args(parser.parse_args(["host"]))
    assert captured["framer"] == "rtu"


async def test_message_spacing_is_passed_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # message_spacing is a device property the tool sets, not a CLI argument.
    captured: dict[str, Any] = {}

    async def fake(target: str, **kwargs: Any) -> str:
        captured.update(kwargs)
        return "conn"

    monkeypatch.setattr(tmodbus_backend, "connect_tcp", lambda t, **k: fake(t, **k))
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


class _Alarm(IntFlag):
    OVER_TEMPERATURE = 0x01
    LOW_FLOW = 0x02
    SENSOR_FAULT = 0x04


class _Meter(Component):
    temperature = gauge(0, 0.1, unit="°C")
    count = integer(1, signed=False)
    state = enum(2, _State)
    alarm = flags(3, _Alarm)
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


def _read_meter(alarm: int = 0x05) -> _Meter:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 235, 1: 7, 2: 1, 3: alarm})
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


async def test_field_rows_renders_a_flag_by_its_set_bits() -> None:
    """An IntFlag names its set bits; ``int.__str__`` would print a number."""
    meter = _read_meter()
    await meter.async_update()

    assert str(meter.alarm) == "5"  # what the generic path would have printed
    assert dict(field_rows(meter))["alarm"] == "over_temperature|sensor_fault"


async def test_field_rows_renders_an_empty_flag_as_none() -> None:
    meter = _read_meter(alarm=0)
    await meter.async_update()
    assert dict(field_rows(meter))["alarm"] == "none"


async def test_field_rows_reports_flag_bits_the_type_does_not_name() -> None:
    """IntFlag keeps unnamed bits; a fault word must not hide one."""
    meter = _read_meter(alarm=0x02 | 0x80)
    await meter.async_update()
    assert dict(field_rows(meter))["alarm"] == "low_flow|0x80"


def test_field_rows_unread_renders_placeholder_without_a_unit() -> None:
    rows = dict(field_rows(_read_meter()))  # never updated
    assert rows["temperature"] == "—"  # nothing was measured, so no unit
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


class _Cell(Component):
    """One repeated sub-unit, modelled at instance 0's addresses."""

    voltage = gauge(10, 0.001, signed=False, unit="V")


class _Battery(Component):
    total_voltage = gauge(0, 0.1, signed=False, unit="V")
    cells = repeating_group(2, _Cell, stride=2)


async def _read_battery() -> _Battery:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 4832, 10: 3300, 12: 3298})
    battery = _Battery(unit)
    await battery.async_update()
    return battery


async def test_group_rows_returns_each_group_with_its_instances() -> None:
    battery = await _read_battery()
    groups = dict(group_rows(battery))

    assert list(groups) == ["cells"]
    assert [cell.voltage for cell in groups["cells"]] == [3.3, 3.298]


async def test_group_rows_does_not_report_groups_as_plain_fields() -> None:
    """A group is a list of sub-components, not a value, so it is not a row."""
    battery = await _read_battery()
    assert "cells" not in dict(field_rows(battery))


def test_print_component_skips_component_accessors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A dump lists device values, not the component's own handles."""

    class Meter(Component):
        voltage = gauge(0, 0.1, unit="V")

    unit = MockModbusConnection().for_unit(1)
    unit.holding[0] = 2301
    meter = Meter(unit)
    print_component(meter)
    out = capsys.readouterr().out
    assert "voltage" in out
    assert "modbus_unit" not in out


async def test_print_component_renders_a_repeating_group_as_sub_blocks() -> None:
    battery = await _read_battery()
    buffer = io.StringIO()
    print_component(battery, title="Battery", file=buffer)
    lines = buffer.getvalue().splitlines()

    assert lines[0] == "Battery"
    assert "  total_voltage  483.2 V" in lines
    # Each instance is its own indented block, numbered from 1.
    assert "  cells[1]" in lines
    assert "  cells[2]" in lines
    assert "    voltage  3.3 V" in lines
    assert "    voltage  3.298 V" in lines


async def test_print_component_indents_a_whole_block() -> None:
    """``indent`` prefixes every line, nested sub-blocks included."""
    battery = await _read_battery()
    buffer = io.StringIO()
    print_component(battery, title="Battery", file=buffer, indent="| ")
    lines = [line for line in buffer.getvalue().splitlines() if line]

    assert all(line.startswith("| ") for line in lines)
    assert "|   cells[1]" in lines


async def test_print_component_without_groups_is_unchanged() -> None:
    """A component with no repeating group prints exactly as it always did."""
    meter = _read_meter()
    await meter.async_update()
    buffer = io.StringIO()
    print_component(meter, title="Sensors", file=buffer)
    assert not buffer.getvalue().endswith("\n\n")


async def test_print_component_defaults_title_to_class_name() -> None:
    meter = _read_meter()
    await meter.async_update()
    buffer = io.StringIO()
    print_component(meter, file=buffer)
    assert buffer.getvalue().splitlines()[0] == "_Meter"


async def test_field_rows_omits_a_field_restrict_fields_dropped() -> None:
    """A dropped field never reads, so an empty row would read as an empty read."""
    meter = _read_meter()
    meter.restrict_fields(["temperature", "relay"])
    await meter.async_update()
    rows = dict(field_rows(meter))

    assert rows["temperature"] == "23.5 °C"
    assert "count" not in rows
    assert "state" not in rows


async def test_field_rows_reads_a_manual_component() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 235, 1: 7})
    manual = ManualComponent(unit)
    manual.add("temperature", gauge(0, 0.1, unit="°C"))
    manual.add("count", integer(1))
    await manual.async_update()

    assert field_rows(manual) == [("temperature", "23.5 °C"), ("count", "7")]


def test_field_rows_renders_an_unread_manual_field() -> None:
    manual = ManualComponent(MockModbusConnection().for_unit(1))
    manual.add("temperature", gauge(0, 0.1, unit="°C"))
    assert field_rows(manual) == [("temperature", "—")]


async def test_group_rows_returns_a_manual_components_groups() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({10: 3300, 12: 3298})
    manual = ManualComponent(unit)
    manual.add("cells", repeating_group(2, _Cell, stride=2))
    await manual.async_update()

    groups = dict(group_rows(manual))
    assert [cell.voltage for cell in groups["cells"]] == [3.3, 3.298]
