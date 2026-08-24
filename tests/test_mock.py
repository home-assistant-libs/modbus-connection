"""Tests for the in-memory mock backend and its pytest fixtures.

The ``mock_modbus_connection`` / ``mock_modbus_unit`` fixtures come from the
``modbus_connection.pytest_plugin`` entry point — no conftest wiring here.
"""

from __future__ import annotations

import pytest

from modbus_connection import (
    ClientClosedError,
    ModbusConnection,
    ModbusConnectionError,
    ModbusExceptionError,
    ModbusUnit,
)
from modbus_connection.mock import (
    MockModbusConnection,
    MockModbusUnit,
    ReadEvent,
    WriteEvent,
)
from modbus_connection.model import Component, integer


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
    mock_modbus_unit.holding[2] = [0x0001, 0x86A0]
    assert await mock_modbus_unit.read_holding_registers(2, 2) == [0x0001, 0x86A0]


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
        WriteEvent("holding", 10, [99], 0x06),
        WriteEvent("coil", 0, [True, False], 0x0F),
    ]


async def test_write_event_carries_the_function_code(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    events: list[WriteEvent] = []
    mock_modbus_unit.on_write(events.append)

    await mock_modbus_unit.write_register(0, 1)
    await mock_modbus_unit.write_registers(1, [2, 3])
    await mock_modbus_unit.write_coil(0, True)
    await mock_modbus_unit.write_coils(1, [False])
    await mock_modbus_unit.mask_write_register(0, and_mask=0, or_mask=1)

    assert [e.function_code for e in events] == [0x06, 0x10, 0x05, 0x0F, 0x16]


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


# -- read log -----------------------------------------------------------------


async def test_read_events_record_every_block(mock_modbus_unit: MockModbusUnit) -> None:
    """Each read is logged with its space, start address and width."""
    await mock_modbus_unit.read_holding_registers(10, 4)
    await mock_modbus_unit.read_input_registers(20, 2)
    await mock_modbus_unit.read_coils(0, 8)
    await mock_modbus_unit.read_discrete_inputs(5, 3)

    assert mock_modbus_unit.read_events == [
        ReadEvent("holding", 10, 4),
        ReadEvent("input", 20, 2),
        ReadEvent("coil", 0, 8),
        ReadEvent("discrete_input", 5, 3),
    ]


async def test_read_events_show_the_blocks_a_component_planned(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """The log is how a device library asserts on its own read plan."""

    class _Meter(Component):
        first = integer(0)
        last = integer(3)

    await _Meter(mock_modbus_unit).async_update()

    # Four fields' worth of addresses pooled into one block spanning 0-3.
    assert mock_modbus_unit.read_events == [ReadEvent("holding", 0, 4)]


async def test_read_events_record_a_read_the_device_rejects(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """A rejected read still went out, so it is still logged."""
    mock_modbus_unit.fail_read(5, ModbusExceptionError("nope"))

    with pytest.raises(ModbusExceptionError):
        await mock_modbus_unit.read_holding_registers(4, 3)

    assert mock_modbus_unit.read_events == [ReadEvent("holding", 4, 3)]


async def test_read_events_ignore_a_write(mock_modbus_unit: MockModbusUnit) -> None:
    await mock_modbus_unit.write_register(0, 1)
    assert mock_modbus_unit.read_events == []


# -- write failures -----------------------------------------------------------


async def test_fail_write_raises_and_leaves_value_unchanged(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    events: list[WriteEvent] = []
    mock_modbus_unit.on_write(events.append)
    mock_modbus_unit.holding[40] = 7
    mock_modbus_unit.fail_write(40, ModbusExceptionError(3))

    with pytest.raises(ModbusExceptionError) as excinfo:
        await mock_modbus_unit.write_register(40, 99)
    assert excinfo.value.exception_code == 3
    # The store is untouched and no on_write callback fired.
    assert await mock_modbus_unit.read_holding_registers(40, 1) == [7]
    assert events == []


async def test_fail_write_cleared_with_none(mock_modbus_unit: MockModbusUnit) -> None:
    mock_modbus_unit.fail_write(40, ModbusExceptionError(3))
    mock_modbus_unit.fail_write(40, None)
    await mock_modbus_unit.write_register(40, 99)
    assert await mock_modbus_unit.read_holding_registers(40, 1) == [99]


async def test_fail_write_triggers_on_any_covered_address(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    mock_modbus_unit.fail_write(43, ModbusExceptionError(2))
    # A multi-register write spanning the armed address fails as a whole...
    with pytest.raises(ModbusExceptionError):
        await mock_modbus_unit.write_registers(42, [1, 2, 3])
    assert await mock_modbus_unit.read_holding_registers(42, 3) == [0, 0, 0]
    # ...while a write that doesn't cover it succeeds.
    await mock_modbus_unit.write_registers(50, [1, 2, 3])
    assert await mock_modbus_unit.read_holding_registers(50, 3) == [1, 2, 3]


async def test_fail_write_applies_to_coils(mock_modbus_unit: MockModbusUnit) -> None:
    mock_modbus_unit.fail_write(5, ModbusExceptionError(3), register_type="coil")
    with pytest.raises(ModbusExceptionError):
        await mock_modbus_unit.write_coil(5, True)
    assert await mock_modbus_unit.read_coils(5, 1) == [False]


async def test_fail_write_coil_and_holding_addresses_are_independent(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    # Arming holding 5 must not affect a coil write at 5 (separate data tables).
    mock_modbus_unit.fail_write(5, ModbusExceptionError(3))  # defaults to holding
    await mock_modbus_unit.write_coil(5, True)
    assert await mock_modbus_unit.read_coils(5, 1) == [True]
    with pytest.raises(ModbusExceptionError):
        await mock_modbus_unit.write_register(5, 1)


# -- read failures ------------------------------------------------------------


async def test_fail_read_raises_and_leaves_other_blocks_readable(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    mock_modbus_unit.holding[0] = 7
    mock_modbus_unit.fail_read(1100, ModbusExceptionError(2))

    with pytest.raises(ModbusExceptionError) as excinfo:
        await mock_modbus_unit.read_holding_registers(1100, 4)
    assert excinfo.value.exception_code == 2
    # A read that doesn't cover the armed address is unaffected.
    assert await mock_modbus_unit.read_holding_registers(0, 1) == [7]


async def test_fail_read_cleared_with_none(mock_modbus_unit: MockModbusUnit) -> None:
    mock_modbus_unit.holding[40] = 9
    mock_modbus_unit.fail_read(40, ModbusExceptionError(2))
    mock_modbus_unit.fail_read(40, None)
    assert await mock_modbus_unit.read_holding_registers(40, 1) == [9]


async def test_fail_read_triggers_on_any_covered_address(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    mock_modbus_unit.fail_read(43, ModbusExceptionError(2))
    # A multi-register read spanning the armed address fails as a whole...
    with pytest.raises(ModbusExceptionError):
        await mock_modbus_unit.read_holding_registers(42, 3)
    # ...while a read that doesn't cover it succeeds.
    assert await mock_modbus_unit.read_holding_registers(50, 3) == [0, 0, 0]


# -- an unreachable device ----------------------------------------------------


async def test_fail_requests_covers_every_read_and_write(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """A device that is not answering has no readable address at all."""
    mock_modbus_unit.holding.update({0: 7, 500: 9})
    mock_modbus_unit.input[0] = 7
    mock_modbus_unit.coils[0] = True
    mock_modbus_unit.fail_requests(ModbusConnectionError("device is offline"))

    for read in (
        mock_modbus_unit.read_holding_registers(0, 1),
        mock_modbus_unit.read_holding_registers(500, 1),
        mock_modbus_unit.read_input_registers(0, 1),
        mock_modbus_unit.read_coils(0, 1),
        mock_modbus_unit.read_discrete_inputs(0, 1),
    ):
        with pytest.raises(ModbusConnectionError, match="offline"):
            await read

    with pytest.raises(ModbusConnectionError):
        await mock_modbus_unit.write_register(0, 1)
    with pytest.raises(ModbusConnectionError):
        await mock_modbus_unit.write_coil(0, True)


async def test_fail_requests_cleared_with_none(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    mock_modbus_unit.holding[0] = 7
    mock_modbus_unit.fail_requests(ModbusConnectionError("device is offline"))
    mock_modbus_unit.fail_requests(None)
    assert await mock_modbus_unit.read_holding_registers(0, 1) == [7]


async def test_fail_requests_still_records_the_attempt(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """The request went out, so it is logged — as with a per-address failure."""
    mock_modbus_unit.fail_requests(ModbusConnectionError("device is offline"))

    with pytest.raises(ModbusConnectionError):
        await mock_modbus_unit.read_holding_registers(4, 3)

    assert mock_modbus_unit.read_events == [ReadEvent("holding", 4, 3)]


async def test_fail_requests_is_per_unit(
    mock_modbus_connection: MockModbusConnection,
) -> None:
    """One silent device on a shared gateway does not silence its neighbours."""
    offline = mock_modbus_connection.for_unit(1)
    other = mock_modbus_connection.for_unit(2)
    other.holding[0] = 7
    offline.fail_requests(ModbusConnectionError("device is offline"))

    with pytest.raises(ModbusConnectionError):
        await offline.read_holding_registers(0, 1)
    assert await other.read_holding_registers(0, 1) == [7]


async def test_fail_read_applies_per_table(mock_modbus_unit: MockModbusUnit) -> None:
    mock_modbus_unit.fail_read(5, ModbusExceptionError(2), register_type="input")
    with pytest.raises(ModbusExceptionError):
        await mock_modbus_unit.read_input_registers(5, 1)
    # Holding, coil and discrete-input tables are independent of the armed input.
    assert await mock_modbus_unit.read_holding_registers(5, 1) == [0]
    assert await mock_modbus_unit.read_coils(5, 1) == [False]
    assert await mock_modbus_unit.read_discrete_inputs(5, 1) == [False]


# -- connection lifecycle -----------------------------------------------------


async def test_connects_on_demand_like_a_real_connection(
    mock_modbus_connection: MockModbusConnection, mock_modbus_unit: MockModbusUnit
) -> None:
    # Construction performs no I/O; the first request establishes the link.
    assert mock_modbus_connection.connected is False
    assert await mock_modbus_unit.read_holding_registers(0, 1) == [0]
    assert mock_modbus_connection.connected is True


async def test_close_marks_disconnected_and_io_raises(
    mock_modbus_connection: MockModbusConnection, mock_modbus_unit: MockModbusUnit
) -> None:
    await mock_modbus_connection.connect()
    assert mock_modbus_connection.connected is True
    assert mock_modbus_unit.connected is True
    await mock_modbus_connection.close()
    assert mock_modbus_connection.connected is False
    assert mock_modbus_unit.connected is False
    with pytest.raises(ClientClosedError):
        await mock_modbus_unit.read_holding_registers(0, 1)


async def test_close_is_permanent(
    mock_modbus_connection: MockModbusConnection,
) -> None:
    await mock_modbus_connection.close()
    await mock_modbus_connection.close()  # idempotent
    with pytest.raises(ClientClosedError):
        await mock_modbus_connection.connect()


async def test_connect_is_a_noop_on_an_open_connection(
    mock_modbus_connection: MockModbusConnection, mock_modbus_unit: MockModbusUnit
) -> None:
    await mock_modbus_connection.connect()
    assert mock_modbus_connection.connected is True
    assert await mock_modbus_unit.read_holding_registers(0, 1) == [0]


async def test_simulate_connection_lost_fires_callbacks(
    mock_modbus_connection: MockModbusConnection, mock_modbus_unit: MockModbusUnit
) -> None:
    calls: list[int] = []
    unsub = mock_modbus_unit.on_connection_lost(lambda: calls.append(1))
    mock_modbus_connection.simulate_connection_lost()
    assert calls == [1]
    assert mock_modbus_connection.connected is False
    unsub()  # must not raise


async def test_a_dropped_link_heals_on_the_next_request(
    mock_modbus_connection: MockModbusConnection, mock_modbus_unit: MockModbusUnit
) -> None:
    mock_modbus_connection.simulate_connection_lost()
    assert mock_modbus_connection.connected is False

    assert await mock_modbus_unit.read_holding_registers(0, 1) == [0]
    assert mock_modbus_connection.connected is True


async def test_disconnect_drops_without_firing_callbacks(
    mock_modbus_connection: MockModbusConnection, mock_modbus_unit: MockModbusUnit
) -> None:
    calls: list[int] = []
    mock_modbus_unit.on_connection_lost(lambda: calls.append(1))

    await mock_modbus_connection.disconnect()
    assert mock_modbus_connection.connected is False
    assert calls == []  # a deliberate drop is not a lost connection

    assert await mock_modbus_unit.read_holding_registers(0, 1) == [0]
    assert mock_modbus_connection.connected is True


async def test_unit_disconnect_drops_the_link(
    mock_modbus_connection: MockModbusConnection, mock_modbus_unit: MockModbusUnit
) -> None:
    await mock_modbus_unit.read_holding_registers(0, 1)
    assert mock_modbus_connection.connected is True

    await mock_modbus_unit.disconnect()
    assert mock_modbus_connection.connected is False


async def test_a_dropped_link_stays_dead_once_closed(
    mock_modbus_connection: MockModbusConnection, mock_modbus_unit: MockModbusUnit
) -> None:
    mock_modbus_connection.simulate_connection_lost()
    await mock_modbus_connection.close()
    with pytest.raises(ClientClosedError):
        await mock_modbus_unit.read_holding_registers(0, 1)


async def test_an_absent_device_is_simulated_with_fail_read(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    mock_modbus_unit.fail_read(0, ModbusConnectionError("no route"))
    with pytest.raises(ModbusConnectionError):
        await mock_modbus_unit.read_holding_registers(0, 1)

    mock_modbus_unit.fail_read(0, None)
    assert await mock_modbus_unit.read_holding_registers(0, 1) == [0]


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


# -- per-unit spacing ---------------------------------------------------------


def test_set_message_spacing_records_the_value(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    assert mock_modbus_unit.message_spacing == 0.0
    mock_modbus_unit.set_message_spacing(0.25)
    assert mock_modbus_unit.message_spacing == 0.25


def test_set_message_spacing_rejects_negative(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    with pytest.raises(ValueError):
        mock_modbus_unit.set_message_spacing(-0.1)
