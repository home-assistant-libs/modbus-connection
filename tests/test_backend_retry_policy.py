"""What the backend libraries are and are not allowed to retry beneath us.

Neither transport may replay a request on its own — except for a device that
answers busy, which tmodbus retransmits natively; this module pins both halves.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import ModuleType

import pytest
from pymodbus.exceptions import ModbusIOException
from pymodbus.pdu import ModbusPDU
from tenacity import AsyncRetrying, retry_never, stop_after_delay, wait_fixed
from tmodbus.exceptions import IllegalDataAddressError, ServerDeviceBusyError
from tmodbus.pdu import ReadHoldingRegistersPDU
from tmodbus.server import ModbusRequestRouter

import modbus_connection.pymodbus as pymodbus_backend
import modbus_connection.tmodbus as tmodbus_backend
from modbus_connection import ModbusConnection, ModbusExceptionError, ModbusTcpParams

from .conftest import HOLDING, UNIT_ID
from .modbus_server import serve_router


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


async def test_tmodbus_transport_propagates_response_without_retry() -> None:
    """tmodbus's SmartTransport invokes its base transport only once.

    Counting transport invocations is inherently white-box, so this one reaches
    under SmartTransport; the device-busy tests below drive a real server.
    """
    connection = tmodbus_backend.ModbusConnection(ModbusTcpParams(host="127.0.0.1"))
    client = await connection._create_client()
    transport = client.transport
    calls = 0

    async def fail_once(unit_id: int, pdu: object) -> object:
        nonlocal calls
        calls += 1
        raise IllegalDataAddressError(function_code=3)

    transport.base_transport.send_and_receive = fail_once  # type: ignore[method-assign]
    with pytest.raises(IllegalDataAddressError):
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


@dataclass
class _BusyDevice:
    """Answers busy ``busy_answers`` times then serves; a negative count never stops."""

    busy_answers: int
    reads: int = 0  # every request the device saw, retransmissions included

    def router(self) -> ModbusRequestRouter:
        router = ModbusRequestRouter()

        @router.register(ReadHoldingRegistersPDU)
        async def read(unit_id: int, request: ReadHoldingRegistersPDU) -> list[int]:
            self.reads += 1
            if self.busy_answers:
                self.busy_answers -= 1
                raise ServerDeviceBusyError(request.function_code)
            return [
                HOLDING.get(request.start_address + i, 0)
                for i in range(request.quantity)
            ]

        return router


@asynccontextmanager
async def _busy_connection(
    backend: ModuleType,
    device: _BusyDevice,
    free_port: int,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[ModbusConnection]:
    """A live connection to a device that answers busy off the wire."""
    monkeypatch.setattr(tmodbus_backend, "_RESPONSE_RETRIES", _FAST_RETRIES)
    host = "127.0.0.1"
    async with serve_router(device.router(), host, free_port):
        conn = backend.ModbusConnection(ModbusTcpParams(host=host, port=free_port))
        try:
            yield conn
        finally:
            await conn.close()


async def test_tmodbus_retransmits_a_busy_response_until_the_device_answers(
    monkeypatch: pytest.MonkeyPatch, free_port: int
) -> None:
    """SERVER_DEVICE_BUSY means "not executed, ask again" — tmodbus does."""
    device = _BusyDevice(busy_answers=2)
    async with _busy_connection(
        tmodbus_backend, device, free_port, monkeypatch
    ) as conn:
        read = await conn.for_unit(UNIT_ID).read_holding_registers(0, 1)

    assert read == [HOLDING[0]]
    assert device.busy_answers == 0  # both busy answers were spent
    assert device.reads == 3  # two busy retransmissions, then the real answer


async def test_tmodbus_busy_retransmission_is_bounded(
    monkeypatch: pytest.MonkeyPatch, free_port: int
) -> None:
    """A forever-busy device terminates, and surfaces as busy.

    The retransmission stops at the bound in the strategy this library hands
    tmodbus, and ``reraise`` makes the device's own busy response the error the
    caller sees rather than tmodbus's retries-exhausted timeout.
    """
    device = _BusyDevice(busy_answers=-1)
    async with _busy_connection(
        tmodbus_backend, device, free_port, monkeypatch
    ) as conn:
        with pytest.raises(ModbusExceptionError) as excinfo:
            await conn.for_unit(UNIT_ID).read_holding_registers(0, 1)

    assert excinfo.value.exception_code == 6
    assert device.reads > 1  # it did retransmit before giving up


async def test_pymodbus_raises_a_busy_response_without_retransmitting(
    monkeypatch: pytest.MonkeyPatch, free_port: int
) -> None:
    """The other half of the documented contrast: pymodbus asks exactly once."""
    device = _BusyDevice(busy_answers=-1)
    async with _busy_connection(
        pymodbus_backend, device, free_port, monkeypatch
    ) as conn:
        with pytest.raises(ModbusExceptionError) as excinfo:
            await conn.for_unit(UNIT_ID).read_holding_registers(0, 1)

    assert excinfo.value.exception_code == 6
    assert device.reads == 1
