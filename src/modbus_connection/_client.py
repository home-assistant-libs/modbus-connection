"""The backend-neutral connection base class and its params dataclasses."""

from __future__ import annotations

import asyncio
import ssl
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from ._callbacks import CallbackRegistry
from ._pacing import Pacer
from ._tls import build_tls_context
from .exceptions import ClientClosedError

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

    sslctx: ssl.SSLContext | None = None
    """A ready-made TLS context. When supplied, it is used as-is and overrides
    ``verify``, ``check_hostname``, and the client-certificate fields. Contexts
    can be shared by multiple connections."""


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
    **no I/O**; unsupported (params type, framer) combinations fail here.
    Every unit request establishes the link on demand through ``connect()``
    (the backends' ``connect_*`` factories call it before returning, and the
    owner may call it explicitly), so after a drop the next request
    reconnects. Consumers NEVER receive this object — only a ``ModbusUnit``
    from ``for_unit``. It is held by the connection's OWNER, and only the
    owner tears it down with ``close()`` — which is permanent: any later
    request raises ``ClientClosedError``.
    """

    def __init__(
        self,
        params: ModbusParams,
        *,
        timeout: float = 3,
        message_spacing: float = 0.0,
    ) -> None:
        self._validate_params(params)
        self._params = params
        self._timeout = timeout
        self._pacer = Pacer(message_spacing)
        self._lost_callbacks = CallbackRegistry()
        self._target = _target(params)
        self._closed = False
        self._resolved_tls_context = (
            params.sslctx if isinstance(params, ModbusTlsParams) else None
        )
        # The single in-flight connect attempt shared by concurrent callers.
        self._connect_task: asyncio.Task[None] | None = None
        # The connected backend client; ``None`` whenever the link is down (not
        # yet connected, dropped, or closed).
        self._client: Any = None

    @property
    def connected(self) -> bool:
        return self._client is not None

    async def connect(self) -> None:
        """Establish the connection; a no-op if already connected.

        Builds and connects a fresh backend client from the stored params —
        after a drop the dead client was cleared, so calling this again
        reconnects. Every unit request does this on demand under a
        single-flight guard: concurrent callers share one in-flight attempt
        and its failure, with no time-based backoff. Raises
        ``ModbusConnectionError`` (or ``ModbusTimeoutError``) if the link
        cannot be established, and ``ClientClosedError`` once the connection
        was ``close()``\\d.
        """
        if self._closed:
            raise ClientClosedError("connection is closed")
        if self._client is not None:
            return
        task = self._connect_task
        if task is None:
            task = self._connect_task = asyncio.ensure_future(self._establish())
        try:
            await task
        finally:
            if self._connect_task is task:
                self._connect_task = None

    async def _establish(self) -> None:
        self._client = await self._connect_client()

    async def _tls_context(self) -> ssl.SSLContext:
        """Resolve and cache this connection's TLS context without blocking."""
        params = self._params
        if not isinstance(params, ModbusTlsParams):
            raise TypeError("TLS context requested for non-TLS connection")
        context = self._resolved_tls_context
        if context is not None:
            return context
        context = await asyncio.to_thread(
            build_tls_context,
            params.verify,
            params.check_hostname,
            params.client_cert,
            params.client_key,
            params.client_key_password,
        )
        self._resolved_tls_context = context
        return context

    @abstractmethod
    def for_unit(self, unit_id: int) -> ModbusUnit:
        """Return this backend's unit handle bound to ``unit_id``."""

    def on_connection_lost(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a callback fired when the link drops; returns an unsubscribe."""
        return self._lost_callbacks.subscribe(callback)

    @abstractmethod
    async def close(self) -> None:
        """Tear the connection down — owner only, idempotent, and permanent."""

    # -- backend hooks ----------------------------------------------------------

    @abstractmethod
    def _validate_params(self, params: ModbusParams) -> None:
        """Reject unsupported (params type, framer) combinations at construction."""

    @abstractmethod
    async def _connect_client(self) -> Any:
        """Build, connect, and return a backend client from ``self._params``,
        mapping failures onto the neutral hierarchy."""
