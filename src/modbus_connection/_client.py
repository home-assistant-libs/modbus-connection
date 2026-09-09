"""The backend-neutral connection base class and its params dataclasses."""

from __future__ import annotations

import asyncio
import ssl
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, fields, replace
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from ._callbacks import CallbackRegistry
from ._pacing import Pacer
from ._tls import build_tls_context
from .exceptions import ClientClosedError

if TYPE_CHECKING:
    from ._protocol import ModbusUnit

__all__ = [
    "BaseModbusConnection",
    "ModbusEndpoint",
    "ModbusParams",
    "ModbusSerialParams",
    "ModbusTcpParams",
    "ModbusTlsParams",
    "ModbusUdpParams",
    "resolve_params",
]

# How long disconnect() and close() wait for the request in flight. A healthy
# request answers in milliseconds, so this is long enough for one to finish and
# short enough that a wedged request never holds up recycling the link.
_TEARDOWN_GRACE = 0.5


def _normalize_host(host: str) -> str:
    """Fold the host to lower case without changing its IPv6 scope identifier."""
    address, separator, scope = host.partition("%")
    return address.lower() + separator + scope


type ModbusEndpoint = tuple[str, str, int] | tuple[str, str]
"""Hashable identity of an addressed device: transport, then its address."""

# Tuning two holders of one link may disagree on. It states how a caller wants
# the link run rather than how to open it, so a shared connection reconciles it
# (see ``resolve_params``) instead of refusing to serve both callers.
_TUNING_FIELDS = frozenset({"timeout", "message_spacing", "connect_delay"})

# A half-duplex RS485 adapter needs time to switch direction between frames.
# Home Assistant has applied this gap to every serial Modbus link since 2021,
# so it is the value the field has proven. The inter-frame silence RTU asks for
# is far shorter, and is not what makes a USB adapter reliable.
_SERIAL_MESSAGE_SPACING = 0.03


@dataclass(frozen=True, kw_only=True)
class _ParamsBase(ABC):
    """The tuning every params class carries: how the caller wants the link run.

    The other fields of a params class describe the link itself, and two
    callers must agree on them to share one connection. These three they need
    not agree on.
    """

    timeout: float = 10
    """Per-request timeout in seconds."""

    message_spacing: float | None = None
    """Minimum gap between requests; ``None`` takes the transport default."""

    connect_delay: float = 0.0
    """Pause after the link opens, before the first request uses it."""

    _default_message_spacing: ClassVar[float] = 0.0

    def __post_init__(self) -> None:
        """Validate the tuning."""
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        if self.message_spacing is not None and self.message_spacing < 0:
            raise ValueError("message_spacing must be non-negative")
        if self.connect_delay < 0:
            raise ValueError("connect_delay must be non-negative")

    @property
    def effective_message_spacing(self) -> float:
        """The gap actually applied: the explicit value, or the transport default."""
        if self.message_spacing is None:
            return self._default_message_spacing
        return self.message_spacing

    @property
    @abstractmethod
    def endpoint(self) -> ModbusEndpoint:
        """Hashable identity of the addressed device."""

    def is_compatible_with(self, other: ModbusParams) -> bool:
        """Whether ``other`` describes the same link, tuning aside.

        One link cannot run at two baud rates, so incompatible params cannot
        share a connection. Compatible ones can, once ``resolve_params``
        settles the tuning between them.
        """
        if type(self) is not type(other):
            return False
        return all(
            getattr(self, field.name) == getattr(other, field.name)
            for field in fields(self)
            if field.name not in _TUNING_FIELDS
        )


@dataclass(frozen=True, kw_only=True)
class ModbusTcpParams(_ParamsBase):
    """Connection parameters for a Modbus TCP link."""

    host: str
    """Host name or IP address of the device, folded to lower case."""

    port: int = 502
    """TCP port."""

    framer: Literal["socket", "rtu", "ascii"] = "socket"
    """Wire framing."""

    def __post_init__(self) -> None:
        """Validate the tuning and the wire framing."""
        super().__post_init__()
        if self.framer not in ("socket", "rtu", "ascii"):
            raise ValueError(
                f"unknown framer {self.framer!r}; expected 'socket', 'rtu', or 'ascii'"
            )
        object.__setattr__(self, "host", _normalize_host(self.host))

    @property
    def endpoint(self) -> tuple[str, str, int]:
        """Hashable identity of the addressed device: transport, host, and port.

        Two params objects with equal endpoints point at the same device even
        when link settings such as ``framer`` differ.
        """
        return ("tcp", self.host, self.port)


@dataclass(frozen=True, kw_only=True)
class ModbusUdpParams(_ParamsBase):
    """Connection parameters for a Modbus UDP link."""

    host: str
    """Host name or IP address of the device, folded to lower case."""

    port: int = 502
    """UDP port."""

    framer: Literal["socket", "rtu", "ascii"] = "socket"
    """Wire framing."""

    def __post_init__(self) -> None:
        """Validate the tuning and the wire framing."""
        super().__post_init__()
        if self.framer not in ("socket", "rtu", "ascii"):
            raise ValueError(
                f"unknown framer {self.framer!r}; expected 'socket', 'rtu', or 'ascii'"
            )
        object.__setattr__(self, "host", _normalize_host(self.host))

    @property
    def endpoint(self) -> tuple[str, str, int]:
        """Hashable identity of the addressed device: transport, host, and port.

        Two params objects with equal endpoints point at the same device even
        when link settings such as ``framer`` differ.
        """
        return ("udp", self.host, self.port)


@dataclass(frozen=True, kw_only=True)
class ModbusTlsParams(_ParamsBase):
    """Connection parameters for a Modbus/TLS (Modbus Security) link."""

    host: str
    """Host name or IP address of the device, folded to lower case."""

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

    def __post_init__(self) -> None:
        """Validate the tuning and fold the host to lower case."""
        super().__post_init__()
        object.__setattr__(self, "host", _normalize_host(self.host))

    @property
    def endpoint(self) -> tuple[str, str, int]:
        """Hashable identity of the addressed device: transport, host, and port.

        Two params objects with equal endpoints point at the same device even
        when the TLS settings differ. The transport tag is ``"tcp"``: a TLS
        link and a plain-TCP link to the same host and port target the same
        TCP endpoint, hence the same device.
        """
        return ("tcp", self.host, self.port)

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
class ModbusSerialParams(_ParamsBase):
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

    _default_message_spacing: ClassVar[float] = _SERIAL_MESSAGE_SPACING

    def __post_init__(self) -> None:
        """Validate the tuning and the serial framing."""
        super().__post_init__()
        if self.framer not in ("rtu", "ascii"):
            raise ValueError(
                f"unknown serial framer {self.framer!r}; expected 'rtu' or 'ascii'"
            )

    @property
    def endpoint(self) -> tuple[str, str]:
        """Hashable identity of the addressed serial port: transport and device.

        Two params objects with equal endpoints point at the same serial port
        even when line settings such as ``baudrate``, ``parity``, or ``framer``
        differ. The device path is compared verbatim; aliases of the same port
        (e.g. a ``/dev/serial/by-id`` symlink versus ``/dev/ttyUSB0``) are not
        resolved.
        """
        return ("serial", self.device)


type ModbusParams = (
    ModbusTcpParams | ModbusUdpParams | ModbusTlsParams | ModbusSerialParams
)
"""Any of the four params dataclasses."""


def resolve_params(params: Iterable[ModbusParams]) -> ModbusParams:
    """Return one params object honoring the most demanding tuning of each.

    Callers sharing a link get the longest timeout, the widest message spacing
    and the longest connect delay any of them asked for, so none is served less
    carefully than it asked to be.

    Raises ``ValueError`` if ``params`` is empty, or if two of them describe
    different links.
    """
    holders = list(params)
    if not holders:
        raise ValueError("resolve_params needs at least one params object")
    first = holders[0]
    for other in holders[1:]:
        if not first.is_compatible_with(other):
            raise ValueError(f"{first} and {other} describe different links")
    return replace(
        first,
        timeout=max(held.timeout for held in holders),
        message_spacing=max(held.effective_message_spacing for held in holders),
        connect_delay=max(held.connect_delay for held in holders),
    )


def _target(
    params: ModbusTcpParams | ModbusUdpParams | ModbusTlsParams | ModbusSerialParams,
) -> str:
    if isinstance(params, ModbusSerialParams):
        return params.device
    return f"{params.host}:{params.port}"


class BaseModbusConnection(ABC):
    """Represent a shared link to a Modbus network."""

    def __init__(
        self,
        params: (
            ModbusTcpParams | ModbusUdpParams | ModbusTlsParams | ModbusSerialParams
        ),
        *,
        timeout: float | None = None,
        message_spacing: float | None = None,
        connect_delay: float | None = None,
    ) -> None:
        """Open nothing yet; the first unit operation connects.

        ``params`` carries the tuning. The keyword arguments override what it
        says, for a caller that keeps the link settings and the tuning apart.
        """
        if (timeout, message_spacing, connect_delay) != (None, None, None):
            params = replace(
                params,
                timeout=params.timeout if timeout is None else timeout,
                message_spacing=(
                    params.message_spacing
                    if message_spacing is None
                    else message_spacing
                ),
                connect_delay=(
                    params.connect_delay if connect_delay is None else connect_delay
                ),
            )
        self._params = params
        self._timeout = params.timeout
        self._pacer = Pacer(params.effective_message_spacing)
        self._connect_delay = params.connect_delay
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
        if self._connect_delay:
            # Some devices need a pause after the link opens before they answer
            # reliably. Inside the shared flight, so concurrent callers all wait
            # it out rather than racing a half-ready device.
            await asyncio.sleep(self._connect_delay)
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

    def set_params(self, params: ModbusParams) -> bool:
        """Adopt new tuning, returning whether the link must be recycled for it.

        Message spacing takes effect at once, and the connect delay applies to
        the next connect. The timeout is fixed when the backend client is
        built, so a live link keeps the old one until it is replaced: ``True``
        asks the owner to ``disconnect()``.

        Raises ``ValueError`` if ``params`` describes a different link.
        """
        if not self._params.is_compatible_with(params):
            raise ValueError(
                f"parameters for a different link cannot be applied to {self._target}"
            )
        recycle = self.connected and params.timeout != self._timeout
        self._params = params
        self._timeout = params.timeout
        self._connect_delay = params.connect_delay
        self._pacer.set_message_spacing(params.effective_message_spacing)
        return recycle

    async def disconnect(self) -> None:
        """Drop the link; the next request establishes a new one.

        For recycling a link that is up but unusable — a peer that keeps the
        socket open but stops answering. Unlike ``close()``, the connection
        stays usable: existing unit handles and components reconnect on their
        next request. A connection is *lost* when the transport takes it away;
        this is tearing it down, so ``on_connection_lost`` callbacks do not
        fire. A no-op when there is no link.

        Waits out a request that is about to answer, up to
        ``_TEARDOWN_GRACE``; a wedged one is cut. Raises
        ``ModbusConnectionError`` if tearing the old link down fails; the link
        is dropped regardless.
        """
        if (task := self._connect_task) is not None:
            # Wait a shared connect attempt out (shielded, as in close()) so
            # its client is published and disposed of here rather than leaked.
            try:
                await asyncio.shield(task)
            except Exception:
                pass
        async with self._pacer.exclusive(_TEARDOWN_GRACE):
            client = self._client
            if client is None:
                return
            self._client = None
            await self._close_client(client)

    async def close(self) -> None:
        """Close the connection permanently.

        The connection is marked closed first, so no further request can
        start. Waits out a request that is about to answer, up to
        ``_TEARDOWN_GRACE``; a wedged one is cut.
        """
        self._closed = True
        if (task := self._connect_task) is not None:
            # Wait the shared connect attempt out; shielded so cancelling this
            # close doesn't kill the flight for concurrent connect() callers.
            try:
                await asyncio.shield(task)
            except Exception:
                pass
        async with self._pacer.exclusive(_TEARDOWN_GRACE):
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
