"""Tests for the in-memory mock backend and its pytest fixtures.

The ``mock_modbus_connection`` / ``mock_modbus_unit`` fixtures come from the
``modbus_connection.pytest_plugin`` entry point — no conftest wiring here.
"""

from __future__ import annotations

import pytest

from modbus_connection import (
    ModbusConnection,
    ModbusConnectionError,
    ModbusExceptionError,
    ModbusUnit,
)
from modbus_connection.mock import (
    MockModbusConnection,
    MockModbusUnit,
    WriteEvent,
)


def test_satisfies_protocols(
    mock_modbus_connection: MockModbusConnection, mock_modbus_unit: MockModbusUnit
) -> None:
    assert isinstance(mock_modbus_connection, ModbusConnection)
    assert isinstance(mock_modbus_unit, ModbusUnit)


# -- value specs: single / list / callable ------------------------------------


async def test_single_value(mock_modbus_unit: MockModbusUnit) -> None:
    mock_modbus_unit.holding[0] = 1234
    assert await mock_modbus_unit.read_holding_registers(0, 1) == [1234]


async def test_unset_registers_default_to_zero(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    mock_modbus_unit.holding[0] = 7
    assert await mock_modbus_unit.read_holding_registers(0, 3) == [7, 0, 0]


async def test_list_value_spans_consecutive_addresses(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    mock_modbus_unit.holding[2] = [0x0001, 0x86A0]  # uint32 big = 100000
    assert await mock_modbus_unit.read_holding_registers(2, 2) == [0x0001, 0x86A0]
    assert await mock_modbus_unit.read_uint32(2, word_order="big") == 100000


async def test_callable_value_is_evaluated_per_read(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    values = iter([10, 20, 30])
    mock_modbus_unit.holding[5] = lambda: next(values)
    assert await mock_modbus_unit.read_holding_registers(5, 1) == [10]
    assert await mock_modbus_unit.read_holding_registers(5, 1) == [20]
    assert await mock_modbus_unit.read_holding_registers(5, 1) == [30]


async def test_callable_may_simulate_device_exception(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    def boom() -> int:
        raise ModbusExceptionError(2)

    mock_modbus_unit.holding[9] = boom
    with pytest.raises(ModbusExceptionError) as excinfo:
        await mock_modbus_unit.read_holding_registers(9, 1)
    assert excinfo.value.exception_code == 2


# -- typed reads --------------------------------------------------------------


async def test_typed_reads(mock_modbus_unit: MockModbusUnit) -> None:
    mock_modbus_unit.holding[0] = 1234
    mock_modbus_unit.holding[1] = 0xFFFF
    assert await mock_modbus_unit.read_uint16(0) == 1234
    assert await mock_modbus_unit.read_int16(1) == -1

    mock_modbus_unit.holding[4] = [0x4148, 0x0000]  # float32 big = 12.5
    assert await mock_modbus_unit.read_float32(4, word_order="big") == pytest.approx(
        12.5
    )

    mock_modbus_unit.holding[6] = [0x4142, 0x4344, 0x0000]  # "ABCD\x00\x00"
    assert await mock_modbus_unit.read_string(6, 3) == "ABCD"


async def test_input_and_discrete_are_separate_spaces(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    mock_modbus_unit.input[0] = 555
    mock_modbus_unit.discrete_inputs[1] = True
    assert await mock_modbus_unit.read_input_registers(0, 1) == [555]
    assert await mock_modbus_unit.read_discrete_inputs(0, 2) == [False, True]
    # Holding space untouched by the input write.
    assert await mock_modbus_unit.read_holding_registers(0, 1) == [0]


# -- writes -------------------------------------------------------------------


async def test_write_roundtrip(mock_modbus_unit: MockModbusUnit) -> None:
    await mock_modbus_unit.write_register(40, 4242)
    assert await mock_modbus_unit.read_holding_registers(40, 1) == [4242]

    await mock_modbus_unit.write_registers(42, [11, 22, 33])
    assert await mock_modbus_unit.read_holding_registers(42, 3) == [11, 22, 33]

    await mock_modbus_unit.write_coils(70, [True, False, True])
    assert await mock_modbus_unit.read_coils(70, 3) == [True, False, True]


async def test_typed_write_roundtrip(mock_modbus_unit: MockModbusUnit) -> None:
    await mock_modbus_unit.write_uint16(80, 4321)
    assert await mock_modbus_unit.read_uint16(80) == 4321
    await mock_modbus_unit.write_float32(82, -7.25, word_order="little")
    assert await mock_modbus_unit.read_float32(
        82, word_order="little"
    ) == pytest.approx(-7.25)


async def test_mask_write_register(mock_modbus_unit: MockModbusUnit) -> None:
    mock_modbus_unit.holding[1] = 0x12
    await mock_modbus_unit.mask_write_register(1, and_mask=0xF2, or_mask=0x25)
    # (0x12 & 0xF2) | (0x25 & ~0xF2) = 0x12 | 0x05 = 0x17
    assert await mock_modbus_unit.read_holding_registers(1, 1) == [0x17]


# -- write callbacks ----------------------------------------------------------


async def test_on_write_receives_event(mock_modbus_unit: MockModbusUnit) -> None:
    events: list[WriteEvent] = []
    mock_modbus_unit.on_write(events.append)

    await mock_modbus_unit.write_register(10, 99)
    await mock_modbus_unit.write_coils(0, [True, False])

    assert events == [
        WriteEvent("holding", 10, [99]),
        WriteEvent("coil", 0, [True, False]),
    ]


async def test_on_write_can_mock_other_data(mock_modbus_unit: MockModbusUnit) -> None:
    # Writing a command register flips a "ready" flag the device would set.
    def respond(event: WriteEvent) -> None:
        if event.register_type == "holding" and event.address == 0:
            mock_modbus_unit.holding[100] = 1

    mock_modbus_unit.on_write(respond)

    assert await mock_modbus_unit.read_holding_registers(100, 1) == [0]
    await mock_modbus_unit.write_register(0, 5)
    assert await mock_modbus_unit.read_holding_registers(100, 1) == [1]


async def test_on_write_unsubscribe(mock_modbus_unit: MockModbusUnit) -> None:
    events: list[WriteEvent] = []
    unsub = mock_modbus_unit.on_write(events.append)
    unsub()
    await mock_modbus_unit.write_register(0, 1)
    assert events == []


# -- connection lifecycle -----------------------------------------------------


async def test_close_marks_disconnected_and_io_raises(
    mock_modbus_connection: MockModbusConnection, mock_modbus_unit: MockModbusUnit
) -> None:
    assert mock_modbus_connection.connected is True
    assert mock_modbus_unit.connected is True
    await mock_modbus_connection.close()
    assert mock_modbus_connection.connected is False
    assert mock_modbus_unit.connected is False
    with pytest.raises(ModbusConnectionError):
        await mock_modbus_unit.read_holding_registers(0, 1)


async def test_simulate_connection_lost_fires_callbacks(
    mock_modbus_connection: MockModbusConnection, mock_modbus_unit: MockModbusUnit
) -> None:
    calls: list[int] = []
    unsub = mock_modbus_unit.on_connection_lost(lambda: calls.append(1))
    mock_modbus_connection.simulate_connection_lost()
    assert calls == [1]
    assert mock_modbus_connection.connected is False
    unsub()  # must not raise


async def test_for_unit_returns_same_instance(
    mock_modbus_connection: MockModbusConnection,
) -> None:
    assert mock_modbus_connection.for_unit(7) is mock_modbus_connection.for_unit(7)
    assert mock_modbus_connection.for_unit(7) is not mock_modbus_connection.for_unit(8)


# -- exotic function codes ----------------------------------------------------


async def test_exotic_code_unconfigured_raises(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    with pytest.raises(NotImplementedError):
        await mock_modbus_unit.report_server_id()


async def test_set_response_value_and_callable(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    mock_modbus_unit.set_response("report_server_id", b"abc")
    assert await mock_modbus_unit.report_server_id() == b"abc"

    counter = iter([1, 2])
    mock_modbus_unit.set_response("read_exception_status", lambda: next(counter))
    assert await mock_modbus_unit.read_exception_status() == 1
    assert await mock_modbus_unit.read_exception_status() == 2
