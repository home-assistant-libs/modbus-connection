"""tmodbus-backend unit tests that don't need a live server."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from tmodbus.exceptions import (
    FunctionCodeError,
    HeaderMismatchError,
    InvalidResponseError,
)
from tmodbus.exceptions import (
    IllegalDataAddressError as TIllegalDataAddressError,
)
from tmodbus.exceptions import ModbusConnectionError as TModbusConnectionError

import modbus_connection.tmodbus as tmodbus_backend
from modbus_connection import (
    ExceptionCode,
    IllegalDataAddressError,
    ModbusConnectionError,
    ModbusDesyncError,
    ModbusProtocolError,
    ModbusTcpParams,
)
from modbus_connection.tmodbus import ModbusConnection


class _FakeClient:
    """A stand-in tmodbus client: always 'connected', records file-record calls."""

    connected = True

    def __init__(self, read_data: bytes = b"") -> None:
        self._read_data = read_data
        self.read_calls: list[tuple[int, int, int]] = []
        self.write_calls: list[tuple[int, int, bytes]] = []
        self.disconnected = False

    def for_unit_id(self, unit_id: int) -> _FakeClient:
        return self

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        self.disconnected = True

    async def read_file_record(
        self, file_number: int, record_number: int, record_length: int
    ) -> bytes:
        self.read_calls.append((file_number, record_number, record_length))
        return self._read_data

    async def write_file_record(
        self, file_number: int, record_number: int, data: bytes
    ) -> object:
        self.write_calls.append((file_number, record_number, data))
        return object()


@pytest.fixture
def connection_to(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[_FakeClient], ModbusConnection]:
    """Build a ModbusConnection that connects to a fake client, not a device."""

    def build(client: _FakeClient) -> ModbusConnection:
        monkeypatch.setattr(
            tmodbus_backend, "create_async_tcp_client", lambda *a, **k: client
        )
        return ModbusConnection(ModbusTcpParams(host="test"))

    return build


async def test_read_file_record_decodes_to_words(
    connection_to: Callable[[_FakeClient], ModbusConnection],
) -> None:
    client = _FakeClient(b"\x00\x2a\x01\x00")  # words 42 and 256
    unit = connection_to(client).for_unit(1)

    words = await unit.read_file_record(file=4, record=1, length=2)

    assert words == [42, 256]
    assert client.read_calls == [(4, 1, 2)]


async def test_write_file_record_encodes_words_to_payload(
    connection_to: Callable[[_FakeClient], ModbusConnection],
) -> None:
    client = _FakeClient()
    unit = connection_to(client).for_unit(1)

    await unit.write_file_record(file=7, record=9, values=[42, 256])

    assert client.write_calls == [(7, 9, b"\x00\x2a\x01\x00")]


class _FunctionCodeClient(_FakeClient):
    """Answers with the wrong function code in the response."""

    async def read_holding_registers(self, *a: object, **k: object) -> list[int]:
        raise FunctionCodeError("expected 0x03, got 0x83", response_bytes=b"\x83")


class _HeaderMismatchClient(_FakeClient):
    """Answers a different transaction than the one asked."""

    async def read_holding_registers(self, *a: object, **k: object) -> list[int]:
        raise HeaderMismatchError("transaction 7 answered 5", response_bytes=b"")


class _InvalidResponseClient(_FakeClient):
    """A unit client whose reads always fail with an invalid response."""

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        raise InvalidResponseError("bad CRC", response_bytes=b"\x00")


class _RaisingClient(_FakeClient):
    """A unit client whose reads raise a given error."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        raise self._error


async def test_invalid_response_maps_to_protocol_error(
    connection_to: Callable[[_FakeClient], ModbusConnection],
) -> None:
    unit = connection_to(_InvalidResponseClient()).for_unit(1)

    with pytest.raises(ModbusProtocolError):
        await unit.read_holding_registers(0, 1)


@pytest.mark.parametrize(
    "client_class",
    [_FunctionCodeClient, _HeaderMismatchClient],
    ids=["function-code", "header-mismatch"],
)
async def test_mismatched_replies_raise_desync(
    connection_to: Callable[[_FakeClient], ModbusConnection],
    client_class: type[_FakeClient],
) -> None:
    """A reply answering a different exchange drops the link and raises."""
    client = client_class()
    connection = connection_to(client)
    unit = connection.for_unit(1)

    with pytest.raises(ModbusDesyncError) as excinfo:
        await unit.read_holding_registers(0, 1)
    assert isinstance(excinfo.value, ModbusProtocolError)
    assert isinstance(excinfo.value.__cause__, InvalidResponseError)
    assert client.disconnected
    assert not connection.connected


class _DesyncTeardownClient(_FunctionCodeClient):
    """Desynchronizes, then fails teardown too."""

    async def disconnect(self) -> None:
        raise OSError("already gone")


async def test_desync_survives_a_failing_disconnect(
    connection_to: Callable[[_FakeClient], ModbusConnection],
) -> None:
    """A teardown failure doesn't mask the desync; the link is still dropped."""
    connection = connection_to(_DesyncTeardownClient())
    unit = connection.for_unit(1)

    with pytest.raises(ModbusDesyncError):
        await unit.read_holding_registers(0, 1)
    assert not connection.connected


async def test_exception_response_maps_to_the_typed_subclass(
    connection_to: Callable[[_FakeClient], ModbusConnection],
) -> None:
    unit = connection_to(_RaisingClient(TIllegalDataAddressError(0x03))).for_unit(1)

    with pytest.raises(IllegalDataAddressError) as excinfo:
        await unit.read_holding_registers(0, 1)
    assert excinfo.value.exception_code is ExceptionCode.ILLEGAL_DATA_ADDRESS


class _DroppingClient(_FakeClient):
    """A unit client whose reads always fail as a lost connection."""

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        raise TModbusConnectionError("link down")


async def test_request_failure_maps_but_does_not_fire_on_connection_lost(
    connection_to: Callable[[_FakeClient], ModbusConnection],
) -> None:
    # Loss is reported by the transport's on_connection_lost hook, not by a failed
    # request, so a request that hits a dropped link only translates the error.
    conn = connection_to(_DroppingClient())
    calls: list[int] = []
    conn.on_connection_lost(lambda: calls.append(1))
    unit = conn.for_unit(1)

    for _ in range(3):
        with pytest.raises(ModbusConnectionError):
            await unit.read_holding_registers(0, 1)

    assert calls == []


async def test_transport_hook_fires_registered_callbacks(
    connection_to: Callable[[_FakeClient], ModbusConnection],
) -> None:
    conn = connection_to(_FakeClient())
    await conn.connect()  # a loss only counts against a live link
    calls: list[int] = []
    conn.on_connection_lost(lambda: calls.append(1))

    conn._on_connection_lost(TModbusConnectionError("link down"))

    assert calls == [1]


async def test_close_suppresses_on_connection_lost_hook(
    connection_to: Callable[[_FakeClient], ModbusConnection],
) -> None:
    # A deliberate close() also triggers tmodbus's on_connection_lost hook (with a
    # None cause); that is not a lost connection, so it must not fire callbacks.
    conn = connection_to(_FakeClient())
    calls: list[int] = []
    conn.on_connection_lost(lambda: calls.append(1))

    await conn.close()
    conn._on_connection_lost(None)  # the hook close() would drive from the loop

    assert calls == []


async def test_transport_hook_clears_the_client_and_unit_cache(
    connection_to: Callable[[_FakeClient], ModbusConnection],
) -> None:
    # A transport drop downs the connection: the dead client (and the unit-bound
    # clients cached against it) are cleared, so the next request reconnects
    # against a fresh client.
    fake = _FakeClient()
    unit = connection_to(fake).for_unit(1)
    conn = unit._conn
    await unit.read_file_record(file=4, record=1, length=2)  # connect + cache

    conn._on_connection_lost(TModbusConnectionError("link down"))

    assert conn.connected is False
    assert conn._unit_clients == {}
