"""An unknown framer name is rejected up front, before any I/O is attempted."""

from __future__ import annotations

import pytest

from modbus_connection.pymodbus import connect_serial as pymodbus_connect_serial
from modbus_connection.pymodbus import connect_tcp as pymodbus_connect_tcp
from modbus_connection.pymodbus import connect_udp as pymodbus_connect_udp
from modbus_connection.tmodbus import connect_serial as tmodbus_connect_serial
from modbus_connection.tmodbus import connect_tcp as tmodbus_connect_tcp


async def test_pymodbus_tcp_rejects_unknown_framer() -> None:
    with pytest.raises(ValueError, match="unknown framer"):
        await pymodbus_connect_tcp("127.0.0.1", framer="bogus")  # type: ignore[arg-type]


async def test_pymodbus_udp_rejects_unknown_framer() -> None:
    with pytest.raises(ValueError, match="unknown framer"):
        await pymodbus_connect_udp("127.0.0.1", framer="bogus")  # type: ignore[arg-type]


async def test_pymodbus_serial_rejects_unknown_framer() -> None:
    with pytest.raises(ValueError, match="unknown serial framer"):
        await pymodbus_connect_serial("/dev/null", framer="socket")  # type: ignore[arg-type]


async def test_tmodbus_tcp_rejects_unknown_framer() -> None:
    with pytest.raises(ValueError, match="unknown framer"):
        await tmodbus_connect_tcp("127.0.0.1", framer="bogus")  # type: ignore[arg-type]


async def test_tmodbus_serial_rejects_unknown_framer() -> None:
    with pytest.raises(ValueError, match="unknown serial framer"):
        await tmodbus_connect_serial("/dev/null", framer="socket")  # type: ignore[arg-type]
