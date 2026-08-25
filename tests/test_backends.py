"""End-to-end + parity tests: both backends against one real server."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from tmodbus.pdu import CommEventCounterResponse, GetCommEventCounterPDU
from tmodbus.server import ModbusRequestRouter

from modbus_connection import (
    ClientClosedError,
    IllegalDataAddressError,
    IllegalFunctionError,
    ModbusConnection,
    ModbusTcpParams,
    ModbusUnit,
)
from modbus_connection.pymodbus import PymodbusConnection
from modbus_connection.pymodbus import connect_tcp as pymodbus_connect_tcp
from modbus_connection.tmodbus import TmodbusConnection
from modbus_connection.tmodbus import connect_tcp as tmodbus_connect_tcp

from .conftest import (
    BUS_MESSAGE_COUNT,
    COILS,
    COMM_EVENT_LOG,
    DEVICE_ID,
    DISCRETE,
    HOLDING,
    INPUT,
    UNIT_ID,
    drop_link,
)
from .modbus_server import serve_router

BACKENDS = ["pymodbus", "tmodbus"]


async def _connect(backend: str, host: str, port: int) -> ModbusConnection:
    if backend == "pymodbus":
        return await pymodbus_connect_tcp(host, port=port)
    return await tmodbus_connect_tcp(host, port=port)


@pytest.fixture(params=BACKENDS)
async def unit(
    request: pytest.FixtureRequest, modbus_server: tuple[str, int]
) -> AsyncIterator[tuple[str, ModbusUnit, ModbusConnection]]:
    backend = request.param
    host, port = modbus_server
    conn = await _connect(backend, host, port)
    try:
        yield backend, conn.for_unit(UNIT_ID), conn
    finally:
        await conn.close()


# -- raw I/O ------------------------------------------------------------------


async def test_read_holding_registers(unit: tuple[str, ModbusUnit, Any]) -> None:
    _, u, _ = unit
    assert await u.read_holding_registers(0, 2) == [HOLDING[0], HOLDING[1]]


async def test_read_input_registers(unit: tuple[str, ModbusUnit, Any]) -> None:
    _, u, _ = unit
    assert await u.read_input_registers(0, 2) == [INPUT[0], INPUT[1]]


async def test_read_coils(unit: tuple[str, ModbusUnit, Any]) -> None:
    _, u, _ = unit
    assert await u.read_coils(0, 3) == [COILS[0], COILS[1], COILS[2]]
    assert await u.read_coils(56, 1) == [True]


async def test_read_discrete_inputs(unit: tuple[str, ModbusUnit, Any]) -> None:
    _, u, _ = unit
    assert await u.read_discrete_inputs(0, 3) == [
        DISCRETE[0],
        DISCRETE[1],
        DISCRETE[2],
    ]


async def test_write_register_roundtrip(unit: tuple[str, ModbusUnit, Any]) -> None:
    _, u, _ = unit
    await u.write_register(40, 4242)
    assert await u.read_holding_registers(40, 1) == [4242]


async def test_write_registers_roundtrip(unit: tuple[str, ModbusUnit, Any]) -> None:
    _, u, _ = unit
    await u.write_registers(42, [11, 22, 33])
    assert await u.read_holding_registers(42, 3) == [11, 22, 33]


async def test_write_coil_roundtrip(unit: tuple[str, ModbusUnit, Any]) -> None:
    _, u, _ = unit
    await u.write_coil(70, True)
    assert await u.read_coils(70, 1) == [True]


async def test_write_coils_roundtrip(unit: tuple[str, ModbusUnit, Any]) -> None:
    _, u, _ = unit
    await u.write_coils(72, [True, False, True])
    assert await u.read_coils(72, 3) == [True, False, True]


# -- device identification (FC43/14) ------------------------------------------


async def test_read_device_identification(unit: tuple[str, ModbusUnit, Any]) -> None:
    _, u, _ = unit
    assert await u.read_device_identification() == DEVICE_ID


# -- diagnostics and comm-event counters (FC08/FC0B/FC0C) ---------------------


async def test_diagnostics_loops_back_query_data(
    unit: tuple[str, ModbusUnit, Any],
) -> None:
    _, u, _ = unit
    assert await u.diagnostics(0x0000, 0x1234) == 0x1234


async def test_diagnostics_reads_a_counter(unit: tuple[str, ModbusUnit, Any]) -> None:
    _, u, _ = unit
    assert await u.diagnostics(0x000B) == BUS_MESSAGE_COUNT


async def test_get_comm_event_counter(unit: tuple[str, ModbusUnit, Any]) -> None:
    _, u, _ = unit
    assert await u.get_comm_event_counter() == (True, len(COMM_EVENT_LOG))


@pytest.mark.parametrize("backend", BACKENDS)
async def test_get_comm_event_counter_reports_a_busy_device(
    backend: str, free_port: int
) -> None:
    """A status word of 0xFFFF means a program command is still running."""
    router = ModbusRequestRouter()

    @router.register(GetCommEventCounterPDU)
    async def counter(
        uid: int, request: GetCommEventCounterPDU
    ) -> CommEventCounterResponse:
        return CommEventCounterResponse(status=0xFFFF, event_count=3)

    host = "127.0.0.1"
    async with serve_router(router, host, free_port):
        conn = await _connect(backend, host, free_port)
        try:
            assert await conn.for_unit(UNIT_ID).get_comm_event_counter() == (False, 3)
        finally:
            await conn.close()


async def test_get_comm_event_log(unit: tuple[str, ModbusUnit, Any]) -> None:
    _, u, _ = unit
    assert await u.get_comm_event_log() == COMM_EVENT_LOG


async def test_diagnostics_refused_raises(unit: tuple[str, ModbusUnit, Any]) -> None:
    """The server answers sub-function 0x02 with exception code 1."""
    _, u, _ = unit
    with pytest.raises(IllegalFunctionError):
        await u.diagnostics(0x0002)


# -- error semantics ----------------------------------------------------------


async def test_illegal_address_raises(unit: tuple[str, ModbusUnit, Any]) -> None:
    _, u, _ = unit
    with pytest.raises(IllegalDataAddressError) as excinfo:
        await u.read_holding_registers(9999, 1)
    assert excinfo.value.exception_code == 2  # the pre-enum idiom keeps working


async def test_error_message_names_the_operation(
    unit: tuple[str, ModbusUnit, Any],
) -> None:
    """A raw request has no ``block``; the message says what was asked for."""
    _, u, _ = unit
    with pytest.raises(IllegalDataAddressError) as excinfo:
        await u.read_holding_registers(9999, 1)
    assert "read_holding_registers(9999, 1)" in str(excinfo.value)
    assert excinfo.value.block is None


# -- connection surface -------------------------------------------------------


async def test_connected_property(unit: tuple[str, ModbusUnit, Any]) -> None:
    _, u, conn = unit
    assert conn.connected is True
    assert u.connected is True


async def test_for_unit_returns_unit(unit: tuple[str, ModbusUnit, Any]) -> None:
    _, _, conn = unit
    other = conn.for_unit(UNIT_ID)
    assert isinstance(other, ModbusUnit)


async def test_on_connection_lost_unsubscribe(
    unit: tuple[str, ModbusUnit, Any],
) -> None:
    _, u, _ = unit
    calls: list[int] = []
    unsub = u.on_connection_lost(lambda: calls.append(1))
    unsub()  # must not raise; callback now detached


# -- parity: both backends agree on the same reads ----------------------------


async def test_parity_across_backends(modbus_server: tuple[str, int]) -> None:
    host, port = modbus_server
    results: dict[str, Any] = {}
    for backend in BACKENDS:
        conn = await _connect(backend, host, port)
        try:
            u = conn.for_unit(UNIT_ID)
            results[backend] = {
                "hr": await u.read_holding_registers(0, 6),
                "coils": await u.read_coils(0, 3),
                "discrete": await u.read_discrete_inputs(0, 3),
            }
        finally:
            await conn.close()
    assert results["pymodbus"] == results["tmodbus"]


@pytest.mark.parametrize("backend", BACKENDS)
async def test_connection_stores_its_params(
    modbus_server: tuple[str, int], backend: str
) -> None:
    # The factories build the shared params dataclass the connection was opened
    # from and the connection carries it.
    host, port = modbus_server
    conn = await _connect(backend, host, port)
    try:
        assert conn._params == ModbusTcpParams(host=host, port=port)
    finally:
        await conn.close()


@pytest.mark.parametrize("backend", BACKENDS)
async def test_direct_construction_first_request_connects(
    modbus_server: tuple[str, int], backend: str
) -> None:
    # A connection constructs from params alone with no I/O; calling connect()
    # explicitly is optional because the first request establishes the link on
    # demand.
    host, port = modbus_server
    params = ModbusTcpParams(host=host, port=port)
    conn: ModbusConnection = (
        PymodbusConnection(params)
        if backend == "pymodbus"
        else TmodbusConnection(params)
    )
    try:
        assert conn.connected is False
        # Unit handles are handed out regardless of connection state; the first
        # request connects without an explicit connect() call.
        unit = conn.for_unit(UNIT_ID)
        assert await unit.read_holding_registers(0, 1) == [1234]
        assert conn.connected is True
    finally:
        await conn.close()


@pytest.mark.parametrize("backend", BACKENDS)
async def test_connect_is_a_noop_when_connected(
    modbus_server: tuple[str, int], backend: str
) -> None:
    host, port = modbus_server
    conn = await _connect(backend, host, port)
    try:
        unit = conn.for_unit(UNIT_ID)
        await conn.connect()  # already connected: nothing happens
        assert conn.connected is True
        # The link (and the unit handle riding on it) is undisturbed.
        assert await unit.read_holding_registers(0, 1) == [1234]
    finally:
        await conn.close()


@pytest.mark.parametrize("backend", BACKENDS)
async def test_connect_reestablishes_a_downed_link(
    modbus_server: tuple[str, int], backend: str
) -> None:
    # Once the link is down (a transport drop, not a close()), connect() is no
    # longer a no-op: the owner reconnects by calling it again — with a fresh
    # backend client. Unit handles resolve through the owner, so a handle
    # obtained before the drop keeps working over the new client.
    host, port = modbus_server
    conn = await _connect(backend, host, port)
    try:
        unit = conn.for_unit(UNIT_ID)
        assert await unit.read_holding_registers(0, 1) == [1234]
        await drop_link(conn)
        assert conn.connected is False
        await conn.connect()
        assert conn.connected is True
        assert await unit.read_holding_registers(0, 1) == [1234]
    finally:
        await conn.close()


@pytest.mark.parametrize("backend", BACKENDS)
async def test_next_request_reconnects_after_drop(
    modbus_server: tuple[str, int], backend: str
) -> None:
    # The transport's connection-lost hook clears the dead client; with
    # per-request connect the next request re-establishes the link on its own —
    # no explicit connect() — and a handle obtained before the drop keeps working.
    host, port = modbus_server
    conn = await _connect(backend, host, port)
    try:
        unit = conn.for_unit(UNIT_ID)
        assert await unit.read_holding_registers(0, 1) == [1234]
        if backend == "pymodbus":
            conn._on_trace_connect(False)  # type: ignore[attr-defined]
        else:
            conn._on_connection_lost(None)  # type: ignore[attr-defined]
        assert conn.connected is False
        assert await unit.read_holding_registers(0, 1) == [1234]
        assert conn.connected is True
    finally:
        await conn.close()


@pytest.mark.parametrize("backend", BACKENDS)
async def test_disconnect_recycles_the_link(
    modbus_server: tuple[str, int], backend: str
) -> None:
    # disconnect() drops a link the transport still considers healthy — the
    # recycle for a peer that is up but unresponsive. The connection stays
    # usable: the same unit handle reconnects on its next request, and no
    # on_connection_lost callback fires for a deliberate drop.
    host, port = modbus_server
    conn = await _connect(backend, host, port)
    lost: list[None] = []
    conn.on_connection_lost(lambda: lost.append(None))
    try:
        unit = conn.for_unit(UNIT_ID)
        assert await unit.read_holding_registers(0, 1) == [1234]

        await conn.disconnect()
        assert conn.connected is False
        assert lost == []

        assert await unit.read_holding_registers(0, 1) == [1234]
        assert conn.connected is True
    finally:
        await conn.close()


async def test_unit_disconnect_recycles_the_link(
    unit: tuple[str, ModbusUnit, ModbusConnection],
) -> None:
    # The unit passthrough, for holders of a handle but not the connection.
    _, u, conn = unit
    assert await u.read_holding_registers(0, 1) == [HOLDING[0]]

    await u.disconnect()
    assert conn.connected is False

    assert await u.read_holding_registers(0, 1) == [HOLDING[0]]
    assert conn.connected is True


@pytest.mark.parametrize("backend", BACKENDS)
async def test_disconnect_without_a_link_is_a_noop(
    modbus_server: tuple[str, int], backend: str
) -> None:
    host, port = modbus_server
    conn = await _connect(backend, host, port)
    try:
        await conn.disconnect()
        await conn.disconnect()  # idempotent
        assert conn.connected is False
    finally:
        await conn.close()


@pytest.mark.parametrize("backend", BACKENDS)
async def test_close_does_not_fire_on_connection_lost(
    modbus_server: tuple[str, int], backend: str
) -> None:
    # A deliberate close() also drives the backend's transport disconnect hook;
    # that is not a lost connection, so registered callbacks must not fire —
    # not during close(), and not for a late disconnect event after it.
    host, port = modbus_server
    conn = await _connect(backend, host, port)
    calls: list[int] = []
    conn.on_connection_lost(lambda: calls.append(1))
    assert await conn.for_unit(UNIT_ID).read_holding_registers(0, 1) == [1234]

    await conn.close()
    if backend == "pymodbus":
        conn._on_trace_connect(False)  # type: ignore[attr-defined]
    else:
        conn._on_connection_lost(None)  # type: ignore[attr-defined]

    assert calls == []


@pytest.mark.parametrize("backend", BACKENDS)
async def test_close_is_permanent(modbus_server: tuple[str, int], backend: str) -> None:
    # close() is idempotent and permanent: it never reconnects afterwards, so a
    # later connect() raises ClientClosedError instead of re-establishing.
    host, port = modbus_server
    conn = await _connect(backend, host, port)
    assert await conn.for_unit(UNIT_ID).read_holding_registers(0, 1) == [1234]

    await conn.close()
    await conn.close()  # second close must not raise
    assert conn.connected is False

    with pytest.raises(ClientClosedError):
        await conn.connect()
    assert conn.connected is False
