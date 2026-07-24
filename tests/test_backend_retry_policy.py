"""The wrapper, not either backend library, owns request retry behavior."""

from __future__ import annotations

import pytest
from pymodbus.exceptions import ModbusIOException
from pymodbus.pdu import ModbusPDU
from tmodbus.exceptions import ModbusResponseError

import modbus_connection.pymodbus as pymodbus_backend
import modbus_connection.tmodbus as tmodbus_backend
from modbus_connection import ModbusTcpParams


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
