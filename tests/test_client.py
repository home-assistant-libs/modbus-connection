"""Behavior of the lazily-connecting, self-reconnecting ``ModbusConnection``s.

These tests drive fake backend base clients (no sockets) so the connect/retry
lifecycle is fully deterministic; the end-to-end reads against real servers live
in ``test_backends`` / ``test_pacing`` / ``test_protocol`` and the transport
test modules.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from collections.abc import Callable

import pytest
from tmodbus.exceptions import ModbusConnectionError as TModbusConnectionError
from tmodbus.exceptions import ModbusResponseError, TModbusError

import modbus_connection.pymodbus as pymodbus_backend
import modbus_connection.tmodbus as tmodbus_backend
from modbus_connection import (
    ClientClosedError,
    ModbusConnectionError,
    ModbusExceptionError,
)
from modbus_connection.tmodbus import (
    ModbusConnection,
    ModbusSerialParams,
    ModbusTcpParams,
    ModbusTlsParams,
    ModbusUdpParams,
)


class _IllegalAddress(ModbusResponseError):
    """A concrete Modbus exception response (illegal data address)."""

    error_code = 2

    def __init__(self) -> None:
        super().__init__(error_code=2, function_code=3)


class _FakeBaseClient:
    """A stand-in tmodbus base client with a controllable connect/read lifecycle.

    ``for_unit_id`` returns the same instance, so every unit of a client shares
    this one fake (and its single connection state), mirroring the real client.
    """

    def __init__(
        self,
        *,
        connect_error: Exception | None = None,
        connect_delay: float = 0.0,
    ) -> None:
        self._connected = False
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.read_calls = 0
        self.write_calls = 0
        self._connect_error = connect_error
        self._connect_delay = connect_delay
        # Each read pops the next behavior: an Exception is raised, anything else
        # is returned. Exhausted -> a default reading.
        self.read_behaviors: list[object] = []
        self.write_behaviors: list[object] = []
        self.on_read: Callable[[], None] | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self.connect_calls += 1
        if self._connect_delay:
            await asyncio.sleep(self._connect_delay)
        if self._connect_error is not None:
            raise self._connect_error
        self._connected = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False

    def for_unit_id(self, unit_id: int) -> _FakeBaseClient:
        return self

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        self.read_calls += 1
        if self.on_read is not None:
            self.on_read()
        if self.read_behaviors:
            behavior = self.read_behaviors.pop(0)
            if isinstance(behavior, Exception):
                raise behavior
            return list(behavior)  # type: ignore[arg-type]
        return [1234]

    async def write_single_register(self, address: int, value: int) -> None:
        self.write_calls += 1
        if self.write_behaviors:
            behavior = self.write_behaviors.pop(0)
            if isinstance(behavior, Exception):
                raise behavior


def _install(monkeypatch: pytest.MonkeyPatch, fake: _FakeBaseClient) -> None:
    monkeypatch.setattr(
        tmodbus_backend, "create_async_tcp_client", lambda *a, **k: fake
    )


def _client(fake: _FakeBaseClient, **kwargs: float) -> ModbusConnection:
    return ModbusConnection(ModbusTcpParams(host="127.0.0.1"), **kwargs)  # type: ignore[arg-type]


# -- params dataclasses -------------------------------------------------------


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


def test_tls_params_defaults_and_frozen() -> None:
    params = ModbusTlsParams(host="dev.local")
    assert (params.port, params.verify, params.check_hostname) == (802, True, True)
    assert (params.client_cert, params.client_key, params.client_key_password) == (
        None,
        None,
        None,
    )
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


# -- lazy construction / first request ----------------------------------------


async def test_construction_does_no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeBaseClient()
    _install(monkeypatch, fake)
    client = _client(fake)
    assert client.connected is False
    assert fake.connect_calls == 0


async def test_first_request_connects(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeBaseClient()
    _install(monkeypatch, fake)
    client = _client(fake)
    assert await client.for_unit(1).read_holding_registers(0, 1) == [1234]
    assert fake.connect_calls == 1
    assert client.connected is True


async def test_for_unit_needs_no_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeBaseClient()
    _install(monkeypatch, fake)
    client = _client(fake)
    # Handing out handles is free and does no I/O, even for never-seen unit ids.
    client.for_unit(1)
    client.for_unit(247)
    assert fake.connect_calls == 0


# -- single-flight connect ----------------------------------------------------


async def test_concurrent_requests_share_one_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeBaseClient(connect_delay=0.05)
    _install(monkeypatch, fake)
    client = _client(fake)
    unit_a, unit_b, unit_c = (client.for_unit(i) for i in (1, 2, 3))

    results = await asyncio.gather(
        unit_a.read_holding_registers(0, 1),
        unit_b.read_holding_registers(0, 1),
        unit_c.read_holding_registers(0, 1),
    )

    assert results == [[1234], [1234], [1234]]
    assert fake.connect_calls == 1  # one in-flight attempt shared by all three


async def test_concurrent_waiters_share_connect_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeBaseClient(connect_delay=0.05, connect_error=TModbusError("refused"))
    _install(monkeypatch, fake)
    client = _client(fake)
    unit_a, unit_b = client.for_unit(1), client.for_unit(2)

    async def read(unit: object) -> Exception:
        with pytest.raises(ModbusConnectionError) as excinfo:
            await unit.read_holding_registers(0, 1)  # type: ignore[attr-defined]
        return excinfo.value

    errors = await asyncio.gather(read(unit_a), read(unit_b))

    assert fake.connect_calls == 1  # a single shared attempt, one shared failure
    assert all(isinstance(err, ModbusConnectionError) for err in errors)


async def test_next_request_reconnects_after_connect_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeBaseClient(connect_error=TModbusError("refused"))
    _install(monkeypatch, fake)
    client = _client(fake)

    with pytest.raises(ModbusConnectionError):
        await client.for_unit(1).read_holding_registers(0, 1)

    # A later request starts a fresh single-flight attempt rather than reusing the
    # failed one.
    fake._connect_error = None
    assert await client.for_unit(1).read_holding_registers(0, 1) == [1234]
    assert fake.connect_calls == 2


# -- retry-once on a mid-request connection drop ------------------------------


async def test_retries_once_on_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeBaseClient()
    _install(monkeypatch, fake)
    client = _client(fake)
    # First read drops the link mid-request; the retry on a fresh connection wins.
    fake.read_behaviors = [TModbusConnectionError("reset"), [7]]

    assert await client.for_unit(1).read_holding_registers(0, 1) == [7]
    assert fake.read_calls == 2
    assert fake.connect_calls == 2  # initial connect + reconnect for the retry
    assert fake.disconnect_calls == 1  # the dead link was dropped before retrying


async def test_does_not_retry_write_on_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeBaseClient()
    _install(monkeypatch, fake)
    client = _client(fake)
    fake.write_behaviors = [TModbusConnectionError("response lost")]

    with pytest.raises(ModbusConnectionError):
        await client.for_unit(1).write_register(0, 7)

    assert fake.write_calls == 1
    assert fake.connect_calls == 1
    assert fake.disconnect_calls == 1
    assert client.connected is False


async def test_modbus_exception_response_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeBaseClient()
    _install(monkeypatch, fake)
    client = _client(fake)
    fake.read_behaviors = [_IllegalAddress(), [7]]

    with pytest.raises(ModbusExceptionError) as excinfo:
        await client.for_unit(1).read_holding_registers(0, 1)

    assert excinfo.value.exception_code == 2
    assert fake.read_calls == 1  # propagated immediately, no retry
    assert fake.connect_calls == 1


async def test_drops_and_reconnects_after_final_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeBaseClient()
    _install(monkeypatch, fake)
    client = _client(fake)
    # Both the request and its one retry hit a dead link: the request fails and
    # the link is left dropped.
    fake.read_behaviors = [
        TModbusConnectionError("reset"),
        TModbusConnectionError("still dead"),
    ]

    with pytest.raises(ModbusConnectionError):
        await client.for_unit(1).read_holding_registers(0, 1)

    assert fake.read_calls == 2
    assert client.connected is False  # dropped, so the next request starts clean

    # The next request reconnects from scratch.
    assert await client.for_unit(1).read_holding_registers(0, 1) == [1234]
    assert fake.connect_calls == 3  # initial + retry-reconnect + next-request


# -- close --------------------------------------------------------------------


async def test_close_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeBaseClient()
    _install(monkeypatch, fake)
    client = _client(fake)
    await client.for_unit(1).read_holding_registers(0, 1)

    await client.close()
    await client.close()  # second close must not raise

    assert client.connected is False


async def test_request_after_close_raises_client_closed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeBaseClient()
    _install(monkeypatch, fake)
    client = _client(fake)
    await client.close()

    # The specific ClientClosedError, still a ModbusConnectionError for
    # existing handlers — from requests and from an explicit connect() alike.
    with pytest.raises(ClientClosedError):
        await client.for_unit(1).read_holding_registers(0, 1)
    with pytest.raises(ClientClosedError):
        await client.connect()
    assert fake.connect_calls == 0  # a closed client never connects


async def test_close_is_safe_with_a_request_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeBaseClient()
    _install(monkeypatch, fake)
    client = _client(fake)

    started = asyncio.Event()
    release = asyncio.Event()

    def block() -> None:
        started.set()

    fake.on_read = block
    # Read blocks inside the fake until released, modelling a request in flight.
    orig_read = fake.read_holding_registers

    async def blocking_read(address: int, count: int) -> list[int]:
        result = await orig_read(address, count)
        await release.wait()
        return result

    fake.read_holding_registers = blocking_read  # type: ignore[method-assign]
    task = asyncio.create_task(client.for_unit(1).read_holding_registers(0, 1))
    await started.wait()

    await client.close()  # must not raise while the request is in flight
    release.set()
    await task  # the in-flight request completes without crashing

    with pytest.raises(ClientClosedError):
        await client.for_unit(1).read_holding_registers(0, 1)


# -- pacing across units ------------------------------------------------------


async def test_pacing_applies_across_units_of_one_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeBaseClient()
    _install(monkeypatch, fake)
    spacing = 0.05
    client = _client(fake, message_spacing=spacing)
    unit_a, unit_b = client.for_unit(1), client.for_unit(2)

    start = time.monotonic()
    # Four requests spread over two units: the connection-wide gap holds no matter
    # which unit issues the next request.
    for unit in (unit_a, unit_b, unit_a, unit_b):
        await unit.read_holding_registers(0, 1)
    elapsed = time.monotonic() - start

    assert elapsed >= spacing * 3


# -- construction-time validation -----------------------------------------------
# (the full per-backend params/framer matrix lives in test_framer_guard)


def test_serial_bogus_framer_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown serial framer"):
        ModbusConnection(ModbusSerialParams(device="/dev/null", framer="socket"))  # type: ignore[arg-type]


# -- the pymodbus client shares the lazy semantics ------------------------------


class _FakePdu:
    """A successful pymodbus response PDU carrying one register."""

    registers = [7]

    def isError(self) -> bool:
        return False


class _FakePymodbusBase:
    """A stand-in pymodbus base client with a controllable read lifecycle."""

    def __init__(self) -> None:
        self.connected = False
        self.connect_calls = 0
        self.close_calls = 0
        self.read_calls = 0
        self.write_calls = 0
        # Each read pops the next behavior: an Exception is raised, anything else
        # is returned as the PDU.
        self.read_behaviors: list[object] = []
        self.write_behaviors: list[object] = []

    async def connect(self) -> bool:
        self.connect_calls += 1
        self.connected = True
        return True

    def close(self) -> None:
        self.close_calls += 1
        self.connected = False

    async def read_holding_registers(
        self, address: int, count: int, device_id: int
    ) -> _FakePdu:
        self.read_calls += 1
        if self.read_behaviors:
            behavior = self.read_behaviors.pop(0)
            if isinstance(behavior, Exception):
                raise behavior
        return _FakePdu()

    async def write_register(
        self, address: int, value: int, device_id: int
    ) -> _FakePdu:
        self.write_calls += 1
        if self.write_behaviors:
            behavior = self.write_behaviors.pop(0)
            if isinstance(behavior, Exception):
                raise behavior
        return _FakePdu()


def _udp_client(
    monkeypatch: pytest.MonkeyPatch, fake: _FakePymodbusBase
) -> pymodbus_backend.ModbusConnection:
    monkeypatch.setattr(pymodbus_backend, "AsyncModbusUdpClient", lambda *a, **k: fake)
    return pymodbus_backend.ModbusConnection(ModbusUdpParams(host="127.0.0.1"))


async def test_udp_lazy_first_request_connects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakePymodbusBase()
    client = _udp_client(monkeypatch, fake)
    assert client.connected is False
    assert fake.connect_calls == 0

    assert await client.for_unit(1).read_holding_registers(0, 1) == [7]

    assert fake.connect_calls == 1
    assert client.connected is True


async def test_udp_retries_once_on_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pymodbus.exceptions import ConnectionException

    fake = _FakePymodbusBase()
    client = _udp_client(monkeypatch, fake)
    fake.read_behaviors = [ConnectionException("reset")]

    assert await client.for_unit(1).read_holding_registers(0, 1) == [7]

    assert fake.read_calls == 2
    assert fake.connect_calls == 2  # initial connect + reconnect for the retry
    assert fake.close_calls == 1  # the dead link was dropped before retrying


async def test_udp_does_not_retry_write_on_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pymodbus.exceptions import ConnectionException

    fake = _FakePymodbusBase()
    client = _udp_client(monkeypatch, fake)
    fake.write_behaviors = [ConnectionException("response lost")]

    with pytest.raises(ModbusConnectionError):
        await client.for_unit(1).write_register(0, 7)

    assert fake.write_calls == 1
    assert fake.connect_calls == 1
    assert fake.close_calls == 1
    assert client.connected is False


async def test_udp_close_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePymodbusBase()
    client = _udp_client(monkeypatch, fake)
    await client.for_unit(1).read_holding_registers(0, 1)

    await client.close()
    await client.close()  # second close must not raise

    assert client.connected is False
    with pytest.raises(ClientClosedError):
        await client.for_unit(1).read_holding_registers(0, 1)
