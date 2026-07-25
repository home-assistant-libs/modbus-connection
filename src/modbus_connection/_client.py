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
    """Wire framing."""

    def __post_init__(self) -> None:
        """Validate the wire framing."""
        if self.framer not in ("socket", "rtu", "ascii"):
            raise ValueError(
                f"unknown framer {self.framer!r}; expected 'socket', 'rtu', or 'ascii'"
            )


@dataclass(frozen=True, kw_only=True)
class ModbusUdpParams:
    """Connection parameters for a Modbus UDP link."""

    host: str
    """Host name or IP address of the device."""

    port: int = 502
    """UDP port."""

    framer: Literal["socket", "rtu", "ascii"] = "socket"
    """Wire framing."""

    def __post_init__(self) -> None:
        """Validate the wire framing."""
        if self.framer not in ("socket", "rtu", "ascii"):
            raise ValueError(
                f"unknown framer {self.framer!r}; expected 'socket', 'rtu', or 'ascii'"
            )


@dataclass(frozen=True, kw_only=True)
class ModbusTlsParams:
    """Connection parameters for a Modbus/TLS (Modbus Security) link."""

    host: str
    """Host name or IP address of the device."""

    port: int = 802
    """TLS port."""

    verify: bool | str = True
    """Whether and how to verify the server certificate."""

    check_hostname: bool = True
    """Whether to verify the certificate hostname."""

    client_cert: str | None = None
    """Path to the client certificate."""

    client_key: str | None = None
    """Path to the private key belonging to ``client_cert``."""

    client_key_password: str | None = None
    """Password for ``client_key``, if it is encrypted."""

    sslctx: ssl.SSLContext | None = None
    """TLS context overriding the other TLS options."""

    async def create_ssl_context(self) -> ssl.SSLContext:
        """Return the supplied TLS context or build one from these parameters."""
        if self.sslctx is not None:
            return self.sslctx
        return await asyncio.to_thread(
            build_tls_context,
            self.verify,
            self.check_hostname,
            self.client_cert,
            self.client_key,
            self.client_key_password,
        )


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
    """Serial framing."""

    def __post_init__(self) -> None:
        """Validate the serial framing."""
        if self.framer not in ("rtu", "ascii"):
            raise ValueError(
                f"unknown serial framer {self.framer!r}; expected 'rtu' or 'ascii'"
            )


ModbusParams = ModbusTcpParams | ModbusUdpParams | ModbusTlsParams | ModbusSerialParams


def _target(params: ModbusParams) -> str:
    if isinstance(params, ModbusSerialParams):
        return params.device
    return f"{params.host}:{params.port}"


class BaseModbusConnection(ABC):
    """Represent a shared link to a Modbus network."""

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
        self._closed = False
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

        Raises ``ModbusConnectionError`` if the connection fails and
        ``ClientClosedError`` if the connection was closed.
        """
        if self._closed:
            raise ClientClosedError("connection is closed")
        if self._client is not None:
            return
        task = self._connect_task
        if task is None:
            task = self._connect_task = asyncio.create_task(self._do_connect())
            task.add_done_callback(self._connect_done)
        await asyncio.shield(task)

    def _connect_done(self, task: asyncio.Task[None]) -> None:
        """Clear a completed connect flight and consume an unobserved failure."""
        if self._connect_task is task:
            self._connect_task = None
        if not task.cancelled():
            task.exception()

    async def _do_connect(self) -> None:
        client = await self._connect_client()
        if self._closed:
            # A concurrent close() marked the connection closed while this
            # client was still being established; dispose of it and refuse.
            try:
                await self._close_client(client)
            except Exception:
                pass
            raise ClientClosedError("connection is closed")
        self._client = client

    @abstractmethod
    def for_unit(self, unit_id: int) -> ModbusUnit:
        """Return this backend's unit handle bound to ``unit_id``."""

    def on_connection_lost(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a callback fired when the link drops; returns an unsubscribe."""
        return self._lost_callbacks.subscribe(callback)

    async def close(self) -> None:
        """Close the connection permanently."""
        self._closed = True
        if (task := self._connect_task) is not None:
            # Wait the shared connect attempt out; shielded so cancelling this
            # close doesn't kill the flight for concurrent connect() callers.
            try:
                await asyncio.shield(task)
            except Exception:
                pass
        client = self._client
        if client is None:
            return
        self._client = None
        await self._close_client(client)

    # -- backend hooks ----------------------------------------------------------

    @abstractmethod
    async def _connect_client(self) -> Any:
        """Build and connect a client."""

    @abstractmethod
    async def _close_client(self, client: Any) -> None:
        """Close a client."""
