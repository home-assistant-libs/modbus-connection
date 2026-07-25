"""What the backend libraries are and are not allowed to retry beneath us.

Neither transport may replay a request on its own — except for a device that
answers busy, which tmodbus retransmits natively; this module pins both halves.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest
from pymodbus.exceptions import ModbusIOException
from pymodbus.pdu import ModbusPDU
from tenacity import (
    AsyncRetrying,
    retry_never,
    stop_after_delay,
    stop_never,
    wait_fixed,
)
from tmodbus.exceptions import ModbusResponseError, ServerDeviceBusyError

import modbus_connection.pymodbus as pymodbus_backend
import modbus_connection.tmodbus as tmodbus_backend
from modbus_connection import ModbusExceptionError, ModbusTcpParams

from .conftest import HOLDING, UNIT_ID


async def test_pymodbus_transport_sends_timed_out_request_once() -> None:
    """pymodbus's transaction manager has no retries beneath the wrapper."""
    connection = pymodbus_backend.ModbusConnection(
        ModbusTcpParams(host="127.0.0.1"), timeout=0.001
    )
    client = await connection._create_client()
    transaction = client.ctx
    transaction.transport = object()  # type: ignore[assignment]
    sends = 0

    def count_send(request: ModbusPDU) -> None:
        nonlocal sends
        sends += 1

    transaction.pdu_send = count_send  # type: ignore[method-assign]
    try:
        with pytest.raises(ModbusIOException):
            await transaction.execute(False, ModbusPDU())
    finally:
        transaction.transport = None

    assert transaction.retries == 0
    assert transaction.comm_params.reconnect_delay == 0
    assert sends == 1


class _IllegalAddress(ModbusResponseError):
    """A concrete tmodbus exception response."""

    error_code = 2

    def __init__(self) -> None:
        super().__init__(error_code=2, function_code=3)


async def test_tmodbus_transport_propagates_response_without_retry() -> None:
    """tmodbus's SmartTransport invokes its base transport only once."""
    connection = tmodbus_backend.ModbusConnection(ModbusTcpParams(host="127.0.0.1"))
    client = await connection._create_client()
    transport = client.transport
    calls = 0

    async def fail_once(unit_id: int, pdu: object) -> object:
        nonlocal calls
        calls += 1
        raise _IllegalAddress

    transport.base_transport.send_and_receive = fail_once  # type: ignore[method-assign]
    with pytest.raises(_IllegalAddress):
        await transport.send_and_receive(1, object())  # type: ignore[arg-type]

    assert not transport.auto_reconnect
    assert calls == 1


# -- device busy (0x06) is tmodbus's to retransmit ----------------------------

_FAST_RETRIES = AsyncRetrying(
    retry=retry_never,
    stop=stop_after_delay(0.05),
    wait=wait_fixed(0.001),
    reraise=True,
)


async def _busy(unit_id: int, pdu: object) -> object:
    raise ServerDeviceBusyError(error_code=6, function_code=3)


async def _busy_connection(
    monkeypatch: pytest.MonkeyPatch,
    modbus_server: tuple[str, int],
    answers: list[Callable[[int, object], Awaitable[object]]],
) -> tmodbus_backend.ModbusConnection:
    """A live tmodbus connection whose transport plays ``answers`` in order.

    The behaviors sit under ``SmartTransport``, so tmodbus's own retry strategy
    (and the stop this library gives it) decides what happens next.
    """
    monkeypatch.setattr(tmodbus_backend, "_RESPONSE_RETRIES", _FAST_RETRIES)
    host, port = modbus_server
    conn = tmodbus_backend.ModbusConnection(ModbusTcpParams(host=host, port=port))
    await conn.connect()
    transport = conn._client.transport
    device = transport.base_transport.send_and_receive

    async def play(unit_id: int, pdu: object) -> object:
        return await answers.pop(0)(unit_id, pdu)

    answers.append(device)
    transport.base_transport.send_and_receive = play
    return conn


async def test_tmodbus_retransmits_a_busy_response_until_the_device_answers(
    monkeypatch: pytest.MonkeyPatch, modbus_server: tuple[str, int]
) -> None:
    """SERVER_DEVICE_BUSY means "not executed, ask again" — tmodbus does."""
    answers = [_busy, _busy]
    conn = await _busy_connection(monkeypatch, modbus_server, answers)
    try:
        read = await conn.for_unit(UNIT_ID).read_holding_registers(0, 1)
    finally:
        await conn.close()

    assert read == [HOLDING[0]]
    assert answers == []  # both busy answers were spent before the real one


async def test_tmodbus_busy_retransmission_is_bounded(
    monkeypatch: pytest.MonkeyPatch, modbus_server: tuple[str, int]
) -> None:
    """A forever-busy device terminates: tmodbus retries under a stop of ours.

    tmodbus takes the stop (and wait) from the strategy this library hands it,
    and tenacity's default is ``stop_never`` — so an unbounded strategy would
    hang here rather than surface the busy response.
    """
    assert tmodbus_backend._RESPONSE_RETRIES.stop is not stop_never
    answers: list[Callable[[int, object], Awaitable[object]]] = []
    conn = await _busy_connection(monkeypatch, modbus_server, answers)
    answers[:] = [_busy] * 1000
    try:
        with pytest.raises(ModbusExceptionError) as excinfo:
            await conn.for_unit(UNIT_ID).read_holding_registers(0, 1)
    finally:
        await conn.close()

    assert excinfo.value.exception_code == 6
