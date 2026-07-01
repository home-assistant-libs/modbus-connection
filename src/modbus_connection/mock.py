"""An in-memory mock backend implementing the modbus_connection Protocols.

This is a test double, not a wire backend: it never opens a socket. Reads pull
from per-unit, address-keyed register stores and writes mutate them, so a test
configures device state up front and asserts on it afterwards. It depends only
on the standard library plus this package's own types — no pymodbus, no tmodbus.

Register / coil values are *value specs* — each store entry may be:

- a single value (``store.holding[0] = 1234``),
- a list, occupying consecutive addresses from its key
  (``store.holding[2] = [0x0001, 0x86A0]`` fills addresses 2 and 3), or
- a zero-argument callable, evaluated on every read for dynamic values
  (``store.holding[9] = lambda: next(counter)``). A callable that raises lets a
  test simulate a device-side failure.

Writes additionally fire any callbacks registered with ``unit.on_write(...)``,
so a test can react to a write by mocking other registers (e.g. flip a "ready"
flag once a command register is set). To simulate a device rejecting a write,
arm ``unit.fail_write(address, error)``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from ._callbacks import CallbackRegistry
from .exceptions import ModbusConnectionError

__all__ = [
    "CoilSpec",
    "MockModbusConnection",
    "MockModbusUnit",
    "RegisterSpec",
    "WriteEvent",
]

RegisterSpec = int | list[int] | Callable[[], "int | list[int]"]
"""A holding/input register store value: a single int, a list of consecutive
ints, or a zero-arg callable returning either."""

CoilSpec = bool | list[bool] | Callable[[], "bool | list[bool]"]
"""A coil/discrete-input store value: a single bool, a list, or a zero-arg
callable returning either."""

RegisterType = Literal["holding", "coil"]


@dataclass(frozen=True)
class WriteEvent:
    """A write that just landed on a unit's store, passed to ``on_write`` callbacks.

    ``register_type`` is ``"holding"`` for register writes and ``"coil"`` for coil
    writes. ``values`` holds the written values, already materialized.
    """

    register_type: RegisterType
    address: int
    values: list[int] | list[bool]


def _materialize(
    space: dict[int, Any], convert: Callable[[Any], Any]
) -> dict[int, Any]:
    """Flatten a value-spec store into a plain address -> value mapping.

    Callables are evaluated and lists are spread across consecutive addresses.
    """
    out: dict[int, Any] = {}
    for base, spec in space.items():
        value = spec() if callable(spec) else spec
        if isinstance(value, (list, tuple)):
            for offset, item in enumerate(value):
                out[base + offset] = convert(item)
        else:
            out[base] = convert(value)
    return out


def _read_registers(space: dict[int, Any], address: int, count: int) -> list[int]:
    materialized = _materialize(space, int)
    return [int(materialized.get(address + i, 0)) for i in range(count)]


def _read_bits(space: dict[int, Any], address: int, count: int) -> list[bool]:
    materialized = _materialize(space, bool)
    return [bool(materialized.get(address + i, False)) for i in range(count)]


class MockModbusConnection:
    """An in-memory ``ModbusConnection``. Construct it directly in tests.

    ``for_unit`` returns the same ``MockModbusUnit`` for a given id, so the unit a
    test configures is the unit the code under test reads. ``connected`` starts
    ``True``; ``close`` or ``simulate_connection_lost`` flip it ``False``, after
    which unit I/O raises ``ModbusConnectionError`` like a real dropped link.
    """

    def __init__(self) -> None:
        self._units: dict[int, MockModbusUnit] = {}
        self._connected = True
        self._lost_callbacks = CallbackRegistry()

    @property
    def connected(self) -> bool:
        return self._connected

    def for_unit(self, unit_id: int) -> MockModbusUnit:
        if unit_id not in self._units:
            self._units[unit_id] = MockModbusUnit(self, unit_id)
        return self._units[unit_id]

    def on_connection_lost(self, callback: Callable[[], None]) -> Callable[[], None]:
        return self._lost_callbacks.subscribe(callback)

    async def close(self) -> None:
        self._connected = False

    def simulate_connection_lost(self) -> None:
        """Flip the link down and fire every ``on_connection_lost`` callback."""
        self._connected = False
        self._lost_callbacks.fire()


class MockModbusUnit:
    """An in-memory ``ModbusUnit`` backed by per-space value-spec stores.

    Configure ``holding``, ``input``, ``coils`` and ``discrete_inputs`` directly
    (e.g. ``unit.holding[0] = 1234``). Reads resolve against them; writes mutate
    ``holding`` / ``coils`` and notify ``on_write`` callbacks. The exotic
    function codes (report-server-id, fifo, device-id, ...) raise
    ``NotImplementedError`` until configured via ``set_response``.
    """

    def __init__(self, connection: MockModbusConnection, unit_id: int) -> None:
        self._conn = connection
        self._unit_id = unit_id
        self.holding: dict[int, RegisterSpec] = {}
        self.input: dict[int, RegisterSpec] = {}
        self.coils: dict[int, CoilSpec] = {}
        self.discrete_inputs: dict[int, CoilSpec] = {}
        self._write_callbacks: list[Callable[[WriteEvent], None]] = []
        self._write_failures: dict[tuple[RegisterType, int], Exception] = {}
        self._responses: dict[str, object] = {}

    @property
    def connected(self) -> bool:
        return self._conn.connected

    def _ensure_connected(self) -> None:
        if not self._conn.connected:
            raise ModbusConnectionError("connection is not established")

    # -- test configuration helpers -------------------------------------------

    def on_write(self, callback: Callable[[WriteEvent], None]) -> Callable[[], None]:
        """Register a callback fired after each register/coil write.

        The callback receives a ``WriteEvent`` and runs *after* the store is
        updated, so it can read current state and mutate other registers. Returns
        an unsubscribe.
        """
        self._write_callbacks.append(callback)

        def unsubscribe() -> None:
            try:
                self._write_callbacks.remove(callback)
            except ValueError:
                pass

        return unsubscribe

    def fail_write(
        self,
        address: int,
        error: Exception | None,
        *,
        register_type: RegisterType = "holding",
    ) -> None:
        """Arm (or clear) a failure for writes touching ``address``.

        A matching write raises ``error`` *before* mutating the store, mirroring
        a device that rejects the write: the stored value is left unchanged and
        ``on_write`` callbacks do not fire. The failure persists until cleared
        with ``fail_write(address, None)``.

        ``register_type`` selects the data table — ``"holding"`` (the default,
        covering register writes incl. ``mask_write_register``) or ``"coil"``.
        Coil and holding addresses are independent, so arming one never affects
        the other.
        """
        key = (register_type, address)
        if error is None:
            self._write_failures.pop(key, None)
        else:
            self._write_failures[key] = error

    def set_response(self, method: str, value: object) -> None:
        """Set the canned result for an exotic function code (e.g.
        ``"report_server_id"``). ``value`` may be a plain value or a zero-arg
        callable evaluated per call."""
        self._responses[method] = value

    def _raise_if_write_fails(
        self, register_type: RegisterType, address: int, count: int = 1
    ) -> None:
        for offset in range(count):
            error = self._write_failures.get((register_type, address + offset))
            if error is not None:
                raise error

    def _fire_write(self, event: WriteEvent) -> None:
        for callback in list(self._write_callbacks):
            callback(event)

    def _canned(self, method: str) -> Any:
        if method not in self._responses:
            raise NotImplementedError(
                f"mock has no response configured for {method}(); "
                f"call unit.set_response({method!r}, ...)"
            )
        value = self._responses[method]
        return value() if callable(value) else value

    # -- raw register I/O -----------------------------------------------------

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        self._ensure_connected()
        return _read_registers(self.holding, address, count)

    async def read_input_registers(self, address: int, count: int) -> list[int]:
        self._ensure_connected()
        return _read_registers(self.input, address, count)

    async def write_register(self, address: int, value: int) -> None:
        self._ensure_connected()
        self._raise_if_write_fails("holding", address)
        self.holding[address] = int(value)
        self._fire_write(WriteEvent("holding", address, [int(value)]))

    async def write_registers(self, address: int, values: list[int]) -> None:
        self._ensure_connected()
        ints = [int(v) for v in values]
        self._raise_if_write_fails("holding", address, len(ints))
        for offset, value in enumerate(ints):
            self.holding[address + offset] = value
        self._fire_write(WriteEvent("holding", address, ints))

    # -- raw coil / discrete-input I/O ----------------------------------------

    async def read_coils(self, address: int, count: int) -> list[bool]:
        self._ensure_connected()
        return _read_bits(self.coils, address, count)

    async def read_discrete_inputs(self, address: int, count: int) -> list[bool]:
        self._ensure_connected()
        return _read_bits(self.discrete_inputs, address, count)

    async def write_coil(self, address: int, value: bool) -> None:
        self._ensure_connected()
        self._raise_if_write_fails("coil", address)
        self.coils[address] = bool(value)
        self._fire_write(WriteEvent("coil", address, [bool(value)]))

    async def write_coils(self, address: int, values: list[bool]) -> None:
        self._ensure_connected()
        bools = [bool(v) for v in values]
        self._raise_if_write_fails("coil", address, len(bools))
        for offset, value in enumerate(bools):
            self.coils[address + offset] = value
        self._fire_write(WriteEvent("coil", address, bools))

    # -- full function-code surface -------------------------------------------

    async def mask_write_register(
        self, address: int, and_mask: int, or_mask: int
    ) -> None:  # 0x16
        self._ensure_connected()
        self._raise_if_write_fails("holding", address)
        current = _read_registers(self.holding, address, 1)[0]
        new = (current & and_mask) | (or_mask & ~and_mask)
        self.holding[address] = new
        self._fire_write(WriteEvent("holding", address, [new]))

    async def read_write_registers(
        self,
        read_address: int,
        read_count: int,
        write_address: int,
        write_values: list[int],
    ) -> list[int]:  # 0x17
        await self.write_registers(write_address, write_values)
        return await self.read_holding_registers(read_address, read_count)

    async def read_exception_status(self) -> int:  # 0x07
        self._ensure_connected()
        return int(self._canned("read_exception_status"))

    async def report_server_id(self) -> bytes:  # 0x11
        self._ensure_connected()
        return bytes(self._canned("report_server_id"))

    async def read_fifo_queue(self, address: int) -> list[int]:  # 0x18
        self._ensure_connected()
        return list(self._canned("read_fifo_queue"))

    async def read_device_identification(self) -> dict[int, bytes]:  # 0x2B / 0x0E
        self._ensure_connected()
        return dict(self._canned("read_device_identification"))

    async def read_file_record(
        self, file: int, record: int, length: int
    ) -> list[int]:  # 0x14
        self._ensure_connected()
        return list(self._canned("read_file_record"))

    async def write_file_record(
        self, file: int, record: int, values: list[int]
    ) -> None:  # 0x15
        self._ensure_connected()

    async def diagnostics(self, sub_function: int, data: int = 0) -> int:  # 0x08
        self._ensure_connected()
        return int(self._canned("diagnostics"))

    async def get_comm_event_counter(self) -> tuple[int, int]:  # 0x0B
        self._ensure_connected()
        status, count = self._canned("get_comm_event_counter")
        return int(status), int(count)

    async def get_comm_event_log(self) -> bytes:  # 0x0C
        self._ensure_connected()
        return bytes(self._canned("get_comm_event_log"))

    def on_connection_lost(self, callback: Callable[[], None]) -> Callable[[], None]:
        return self._conn.on_connection_lost(callback)
