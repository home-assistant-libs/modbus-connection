"""End-to-end + parity tests: both backends against one real server."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from modbus_connection import (
    ModbusConnection,
    ModbusExceptionError,
    ModbusUnit,
)
from modbus_connection.pymodbus import connect_tcp as pymodbus_connect_tcp
from modbus_connection.tmodbus import connect_tcp as tmodbus_connect_tcp

from .conftest import COILS, DISCRETE, HOLDING, INPUT, UNIT_ID

BACKENDS = ["pymodbus", "tmodbus"]


async def _connect(backend: str, host: str, port: int) -> ModbusConnection:
    if backend == "pymodbus":
        return await pymodbus_connect_tcp(host, port=port)
    return await tmodbus_connect_tcp(host, port=port, unit_id=UNIT_ID)


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


# -- typed reads --------------------------------------------------------------


async def test_read_uint16(unit: tuple[str, ModbusUnit, Any]) -> None:
    _, u, _ = unit
    assert await u.read_uint16(0) == 1234


async def test_read_int16_negative(unit: tuple[str, ModbusUnit, Any]) -> None:
    _, u, _ = unit
    assert await u.read_int16(1) == -1


async def test_read_uint32_big(unit: tuple[str, ModbusUnit, Any]) -> None:
    _, u, _ = unit
    assert await u.read_uint32(2, word_order="big") == 70000


async def test_read_uint32_little(unit: tuple[str, ModbusUnit, Any]) -> None:
    _, u, _ = unit
    # Same registers read little-word-order: words swapped.
    expected = (HOLDING[3] << 16) | HOLDING[2]
    assert await u.read_uint32(2, word_order="little") == expected


async def test_read_float32_big(unit: tuple[str, ModbusUnit, Any]) -> None:
    _, u, _ = unit
    assert await u.read_float32(4, word_order="big") == pytest.approx(12.5)


async def test_read_string(unit: tuple[str, ModbusUnit, Any]) -> None:
    _, u, _ = unit
    assert await u.read_string(6, 4) == "ABCDEF"


async def test_typed_write_roundtrip(unit: tuple[str, ModbusUnit, Any]) -> None:
    _, u, _ = unit
    await u.write_uint16(80, 4321)
    assert await u.read_uint16(80) == 4321
    await u.write_float32(82, 3.5, word_order="big")
    assert await u.read_float32(82, word_order="big") == pytest.approx(3.5)
    await u.write_float32(84, -7.25, word_order="little")
    assert await u.read_float32(84, word_order="little") == pytest.approx(-7.25)


# -- error semantics ----------------------------------------------------------


async def test_illegal_address_raises(unit: tuple[str, ModbusUnit, Any]) -> None:
    _, u, _ = unit
    with pytest.raises(ModbusExceptionError) as excinfo:
        await u.read_holding_registers(9999, 1)
    assert excinfo.value.exception_code == 2  # illegal data address


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
                "uint16": await u.read_uint16(0),
                "int16": await u.read_int16(1),
                "uint32_big": await u.read_uint32(2),
                "float32": round(await u.read_float32(4), 4),
                "coils": await u.read_coils(0, 3),
                "discrete": await u.read_discrete_inputs(0, 3),
            }
        finally:
            await conn.close()
    assert results["pymodbus"] == results["tmodbus"]
