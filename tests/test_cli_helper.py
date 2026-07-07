"""Tests for the query-helper building blocks (modbus_connection.cli_helper).

Covers the three pieces a query script imports instead of re-implementing:
argument parsing → connection, the read-counting unit wrapper, and the
reflection-based field printer.
"""

from __future__ import annotations

import argparse
import io
from enum import IntEnum
from typing import Any, cast

import pytest

import modbus_connection.tmodbus as tmodbus_backend
from modbus_connection import ModbusConnectionError, ModbusUnit
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


async def test_connect_from_args_dispatches_by_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    async def record(name: str, target: str, **kwargs: Any) -> str:
        calls["name"], calls["target"], calls["kwargs"] = name, target, kwargs
        return "conn"

    for func in ("connect_tcp", "connect_tls", "connect_serial"):
        monkeypatch.setattr(
            tmodbus_backend,
            func,
            lambda target, _f=func, **kw: record(_f, target, **kw),
        )

    await connect_from_args(_parse(["/dev/ttyUSB0", "--transport", "serial"]))
    assert calls["name"] == "connect_serial"
    assert calls["target"] == "/dev/ttyUSB0"
    assert calls["kwargs"]["baudrate"] == 9600

    await connect_from_args(
        _parse(["dev.local", "--transport", "tls", "--tls-ca", "ca"])
    )
    assert calls["name"] == "connect_tls"
    assert calls["kwargs"]["verify"] == "ca"

    await connect_from_args(
        _parse(["dev.local", "--transport", "tls", "--tls-no-verify"])
    )
    assert calls["kwargs"]["verify"] is False

    await connect_from_args(_parse(["1.2.3.4", "--framer", "rtu", "--port", "1502"]))
    assert calls["name"] == "connect_tcp"
    assert calls["kwargs"]["framer"] == "rtu"
    assert calls["kwargs"]["port"] == 1502


def test_unset_port_and_framer_left_to_backend() -> None:
    # Left unset so the backend default applies rather than being forced here.
    args = _parse(["dev.local"])
    assert args.port is None
    assert args.framer is None


# -- CountingUnit -------------------------------------------------------------


async def test_counting_unit_counts_reads_and_delegates() -> None:
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update({0: 11, 1: 22})
    unit.coils.update({0: True})

    counting = CountingUnit(cast(ModbusUnit, unit))
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

    counting = CountingUnit(cast(ModbusUnit, unit))
    meter = _Meter(cast(ModbusUnit, counting))
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
