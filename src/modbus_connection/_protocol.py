"""The backend-neutral Modbus Protocols.

This module defines the contract that every backend (pymodbus, tmodbus, ...)
implements. It imports nothing from any Modbus library and nothing from Home
Assistant. Consumers type against ``ModbusUnit`` / ``ModbusConnection`` and stay
ignorant of which backend produced them.
"""

from collections.abc import Callable
from typing import Protocol, runtime_checkable


@runtime_checkable
class ModbusUnit(Protocol):
    """A stateless handle bound to one unit (unit ID) on a shared connection.

    Holds no buffered state beyond the address. Methods RAISE on any failure
    (timeout, exception response, link down); they never return ``None`` or
    swallow errors. A unit has NO lifecycle methods: a consumer cannot connect
    or close the link it rides on.
    """

    @property
    def connected(self) -> bool: ...

    # raw register I/O
    async def read_holding_registers(self, address: int, count: int) -> list[int]: ...
    async def read_input_registers(self, address: int, count: int) -> list[int]: ...
    async def write_register(self, address: int, value: int) -> None: ...
    async def write_registers(self, address: int, values: list[int]) -> None: ...

    # raw coil / discrete-input I/O
    async def read_coils(self, address: int, count: int) -> list[bool]: ...
    async def read_discrete_inputs(self, address: int, count: int) -> list[bool]: ...
    async def write_coil(self, address: int, value: bool) -> None: ...
    async def write_coils(self, address: int, values: list[bool]) -> None: ...

    # The full Modbus function-code set (complete spec). A backend that doesn't
    # implement a given code raises NotImplementedError.
    async def read_exception_status(self) -> int: ...  # 0x07
    async def report_server_id(self) -> bytes: ...  # 0x11
    async def mask_write_register(
        self, address: int, and_mask: int, or_mask: int
    ) -> None: ...  # 0x16
    async def read_write_registers(
        self,
        read_address: int,
        read_count: int,
        write_address: int,
        write_values: list[int],
    ) -> list[int]: ...  # 0x17
    async def read_fifo_queue(self, address: int) -> list[int]: ...  # 0x18
    async def read_device_identification(self) -> dict[int, bytes]: ...  # 0x2B / 0x0E
    async def read_file_record(
        self, file: int, record: int, length: int
    ) -> list[int]: ...  # 0x14
    async def write_file_record(
        self, file: int, record: int, values: list[int]
    ) -> None: ...  # 0x15
    async def diagnostics(self, sub_function: int, data: int = 0) -> int: ...  # 0x08
    async def get_comm_event_counter(self) -> tuple[int, int]: ...  # 0x0B
    async def get_comm_event_log(self) -> bytes: ...  # 0x0C

    def on_connection_lost(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a callback fired when the link drops; returns an unsubscribe."""


@runtime_checkable
class ModbusConnection(Protocol):
    """A shared, internally-serialized, already-connected link to a Modbus network.

    You never construct or ``connect()`` this — a backend connect function returns
    a live instance (e.g. ``modbus_connection.pymodbus.connect_tcp(...)``).
    Consumers NEVER receive this object — only a ``ModbusUnit``. It is held by the
    connection's OWNER.
    """

    @property
    def connected(self) -> bool: ...

    def for_unit(self, unit_id: int) -> ModbusUnit: ...

    def on_connection_lost(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Owner-level drop callback; returns an unsubscribe."""

    # Teardown — OWNER ONLY. There is no connect(): the instance is already live
    # and reconnects are the owner's job, never the abstraction's.
    async def close(self) -> None: ...
