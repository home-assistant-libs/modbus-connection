"""The shared, backend-neutral connection-params dataclasses.

One frozen, keyword-only dataclass per transport, describing the link rather
than the backend that opens it. Being frozen and hashable, an instance doubles
as a connection identity key — two equal params objects describe the same
physical link. Import them from the top-level package or from either backend
module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "ModbusParams",
    "ModbusSerialParams",
    "ModbusTcpParams",
    "ModbusTlsParams",
    "ModbusUdpParams",
]


@dataclass(frozen=True, kw_only=True)
class ModbusTcpParams:
    """Connection parameters for a Modbus TCP link.

    ``framer`` selects the wire framing: ``"socket"`` for native Modbus TCP
    (MBAP), ``"rtu"`` for RTU-over-TCP — what transparent serial-to-Ethernet
    gateways speak — or ``"ascii"`` for ASCII frames tunnelled over the TCP
    stream (pymodbus client only). Frozen and hashable, so an instance doubles
    as a connection identity key.
    """

    host: str
    port: int = 502
    framer: Literal["socket", "rtu", "ascii"] = "socket"


@dataclass(frozen=True, kw_only=True)
class ModbusUdpParams:
    """Connection parameters for a Modbus UDP link (pymodbus client only).

    UDP carries the same wire framings as TCP; ``framer`` selects ``"socket"``
    for native Modbus (MBAP), ``"rtu"``, or ``"ascii"``. Frozen and hashable,
    so an instance doubles as a connection identity key.
    """

    host: str
    port: int = 502
    framer: Literal["socket", "rtu", "ascii"] = "socket"


@dataclass(frozen=True, kw_only=True)
class ModbusTlsParams:
    """Connection parameters for a Modbus/TLS (Modbus Security) link.

    Server verification — ``verify`` controls how the device's certificate is
    checked (the ``httpx`` convention): ``True`` verifies against the system
    trust store, ``False`` disables verification (self-signed devices), and a
    path (``str``) verifies against a CA bundle file or directory of CAs (e.g.
    to pin a device's own self-signed certificate). ``check_hostname`` gates
    hostname matching while still verifying the certificate; it is ignored when
    ``verify`` is ``False``.

    Client identity (mutual TLS) — ``client_cert`` / ``client_key`` /
    ``client_key_password`` are this side's own certificate, presented to the
    device.

    Frozen and hashable, so an instance doubles as a connection identity key.
    """

    host: str
    port: int = 802
    verify: bool | str = True
    check_hostname: bool = True
    client_cert: str | None = None
    client_key: str | None = None
    client_key_password: str | None = None


@dataclass(frozen=True, kw_only=True)
class ModbusSerialParams:
    """Connection parameters for a Modbus serial link.

    ``framer`` selects the serial framing: ``"rtu"`` for binary Modbus RTU (the
    default) or ``"ascii"`` for the ASCII transmission mode. Frozen and hashable,
    so an instance doubles as a connection identity key.
    """

    device: str
    baudrate: int = 9600
    bytesize: Literal[7, 8] = 8
    parity: Literal["N", "E", "O"] = "N"
    stopbits: Literal[1, 2] = 1
    framer: Literal["rtu", "ascii"] = "rtu"


ModbusParams = ModbusTcpParams | ModbusUdpParams | ModbusTlsParams | ModbusSerialParams
