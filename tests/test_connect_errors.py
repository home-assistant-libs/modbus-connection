"""Backend exceptions raised while connecting or closing map to the neutral type.

A backend whose constructor, ``connect()``, or ``close()``/``disconnect()``
*raises* must surface as ``ModbusConnectionError`` — not as a raw
pymodbus/tmodbus exception leaking through the abstraction.
"""

from __future__ import annotations

import pytest
from pymodbus.exceptions import ModbusException
from tmodbus.exceptions import TModbusError

import modbus_connection.pymodbus as pymodbus_backend
import modbus_connection.tmodbus as tmodbus_backend
from modbus_connection import ModbusConnectionError
from modbus_connection.pymodbus import PymodbusConnection
from modbus_connection.pymodbus import connect_tcp as pymodbus_connect_tcp
from modbus_connection.tmodbus import TmodbusConnection
from modbus_connection.tmodbus import connect_tcp as tmodbus_connect_tcp

# -- pymodbus -----------------------------------------------------------------


async def test_pymodbus_connect_maps_raising_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(self: object) -> bool:
        raise ModbusException("connect blew up")

    monkeypatch.setattr(
        pymodbus_backend.AsyncModbusTcpClient, "connect", boom, raising=True
    )
    with pytest.raises(ModbusConnectionError):
        await pymodbus_connect_tcp("127.0.0.1", port=502)


async def test_pymodbus_connect_maps_raising_constructor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: object, **kwargs: object) -> object:
        raise ModbusException("constructor blew up")

    monkeypatch.setattr(pymodbus_backend, "AsyncModbusTcpClient", boom)
    with pytest.raises(ModbusConnectionError):
        await pymodbus_connect_tcp("127.0.0.1", port=502)


async def test_pymodbus_close_maps_backend_error() -> None:
    class RaisingClient:
        connected = True

        def close(self) -> None:
            raise ModbusException("close blew up")

    conn = PymodbusConnection(RaisingClient())
    with pytest.raises(ModbusConnectionError):
        await conn.close()


# -- tmodbus ------------------------------------------------------------------


async def test_tmodbus_connect_maps_raising_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RaisingClient:
        connected = False

        async def connect(self) -> None:
            # A TModbusError that the old, narrow except clause did not catch.
            raise TModbusError("connect blew up")

    monkeypatch.setattr(
        tmodbus_backend, "create_async_tcp_client", lambda *a, **k: RaisingClient()
    )
    with pytest.raises(ModbusConnectionError):
        await tmodbus_connect_tcp("127.0.0.1", port=502)


async def test_tmodbus_close_maps_backend_error() -> None:
    class RaisingClient:
        connected = True

        async def disconnect(self) -> None:
            raise TModbusError("disconnect blew up")

    conn = TmodbusConnection(RaisingClient())
    with pytest.raises(ModbusConnectionError):
        await conn.close()
