"""The backend-neutral connection base class and its params dataclasses.

The params dataclasses are shared and backend-neutral: one frozen,
keyword-only dataclass per transport, describing the link rather than the
backend that opens it. Being frozen and hashable, an instance doubles as a
connection identity key — two equal params objects describe the same physical
link. Import them from the top-level package or from either backend module.

:class:`BaseModbusConnection` is the abstract surface every backend's
connection type implements; it is exported from the top level as
``modbus_connection.ModbusConnection`` for typing and isinstance checks. A
connection is constructed from the params dataclass alone (no I/O) and
established with ``connect()`` — a no-op when already connected. The base owns
the pieces every backend shares — the stored params, the ``connect()``
lifecycle, and the loss-callback registry — while a subclass supplies the
params-to-client mapping, its unit type, and teardown.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from ._callbacks import CallbackRegistry
from ._pacing import Pacer

if TYPE_CHECKING:
    from ._protocol import ModbusUnit

__all__ = [
    "BaseModbusConnection",
    "ModbusParams",
    "ModbusSerialParams",
    "ModbusTcpParams",
    "ModbusTlsParams",
    "ModbusUdpParams",
]


@dataclass(frozen=True, kw_only=True)
class ModbusTcpParams:
    """Connection parameters for a Modbus TCP link."""

    host: str
    """Host name or IP address of the device."""

    port: int = 502
    """TCP port."""

    framer: Literal["socket", "rtu", "ascii"] = "socket"
    """Wire framing: ``"socket"`` for native Modbus TCP (MBAP), ``"rtu"`` for
    RTU-over-TCP — what transparent serial-to-Ethernet gateways speak — or
    ``"ascii"`` for ASCII frames tunnelled over the TCP stream."""


@dataclass(frozen=True, kw_only=True)
class ModbusUdpParams:
    """Connection parameters for a Modbus UDP link."""

    host: str
    """Host name or IP address of the device."""

    port: int = 502
    """UDP port."""

    framer: Literal["socket", "rtu", "ascii"] = "socket"
    """Wire framing, same choices as TCP: ``"socket"`` for native Modbus
    (MBAP), ``"rtu"``, or ``"ascii"``."""


@dataclass(frozen=True, kw_only=True)
class ModbusTlsParams:
    """Connection parameters for a Modbus/TLS (Modbus Security) link."""

    host: str
    """Host name or IP address of the device."""

    port: int = 802
    """TLS port."""

    verify: bool | str = True
    """How the device's certificate is checked (the ``httpx`` convention):
    ``True`` verifies against the system trust store, ``False`` disables
    verification (self-signed devices), and a path (``str``) verifies against
    a CA bundle file or directory of CAs (e.g. to pin a device's own
    self-signed certificate)."""

    check_hostname: bool = True
    """Match the certificate against the host name while verifying; ignored
    when ``verify`` is ``False``."""

    client_cert: str | None = None
    """Path to this side's own certificate, presented to the device
    (mutual TLS)."""

    client_key: str | None = None
    """Path to the private key belonging to ``client_cert``."""

    client_key_password: str | None = None
    """Password for ``client_key``, if it is encrypted."""


@dataclass(frozen=True, kw_only=True)
class ModbusSerialParams:
    """Connection parameters for a Modbus serial link."""

    device: str
    """Serial port device path (e.g. ``/dev/ttyUSB0``)."""

    baudrate: int = 9600
    """Line speed in baud."""

    bytesize: Literal[7, 8] = 8
    """Data bits per character."""

    parity: Literal["N", "E", "O"] = "N"
    """Parity: none, even, or odd."""

    stopbits: Literal[1, 2] = 1
    """Stop bits per character."""

    framer: Literal["rtu", "ascii"] = "rtu"
    """Serial framing: ``"rtu"`` for binary Modbus RTU (the default) or
    ``"ascii"`` for the ASCII transmission mode."""


ModbusParams = ModbusTcpParams | ModbusUdpParams | ModbusTlsParams | ModbusSerialParams


def _target(params: ModbusParams) -> str:
    if isinstance(params, ModbusSerialParams):
        return params.device
    return f"{params.host}:{params.port}"


class BaseModbusConnection(ABC):
    """A shared, internally-serialized link to a Modbus network.

    The concrete classes are the backends' connection types. Construction takes
    only the params dataclass — the credentials for every connect — and does
    **no I/O**; ``connect()`` establishes the link (the backends' ``connect_*``
    factories do exactly that before returning). Consumers NEVER receive this
    object — only a ``ModbusUnit`` from ``for_unit``. It is held by the
    connection's OWNER, and only the owner tears it down with ``close()``;
    reconnecting after a drop is likewise the owner's job — by calling
    ``connect()`` again.
    """

    def __init__(
        self,
        params: ModbusParams,
        *,
        timeout: float = 3,
        message_spacing: float = 0.0,
    ) -> None:
        self._params = params
        self._timeout = timeout
        self._pacer = Pacer(message_spacing)
        self._lost_callbacks = CallbackRegistry()
        self._target = _target(params)
        # The backend client; built from the params on the first ``connect()``.
        self._client: Any = None

    @property
    def connected(self) -> bool:
        return self._client is not None and bool(self._client.connected)

    async def connect(self) -> None:
        """Establish the connection; a no-op if already connected.

        On the first call this builds the backend client from the stored
        params; after a drop, calling it again reconnects. Raises
        ``ModbusConnectionError`` (or ``ModbusTimeoutError``) if the link
        cannot be established.
        """
        if self.connected:
            return
        if self._client is None:
            self._client = await self._async_create_client()
        await self._connect_client()

    @abstractmethod
    def for_unit(self, unit_id: int) -> ModbusUnit:
        """Return this backend's unit handle bound to ``unit_id``."""

    def on_connection_lost(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a callback fired when the link drops; returns an unsubscribe."""
        return self._lost_callbacks.subscribe(callback)

    @abstractmethod
    async def close(self) -> None:
        """Tear the connection down — owner only."""

    # -- backend hooks ----------------------------------------------------------

    @abstractmethod
    async def _async_create_client(self) -> Any:
        """Build the not-yet-connected backend client from ``self._params``."""

    @abstractmethod
    async def _connect_client(self) -> None:
        """Connect ``self._client``, mapping failures onto the neutral hierarchy."""
