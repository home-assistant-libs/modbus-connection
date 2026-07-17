"""The backend-neutral ``ModbusUnit`` Protocol.

This module defines the unit contract that every backend (pymodbus, tmodbus,
...) implements. It imports nothing from any Modbus library and nothing from
Home Assistant. Consumers type against ``ModbusUnit`` (and against
``modbus_connection.ModbusConnection`` — the connection base class in
``_client`` — for the owner side) and stay ignorant of which backend produced
them.
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

    def set_message_spacing(self, seconds: float) -> None:
        """Set a minimum gap, in seconds, between consecutive requests to THIS
        unit, layered on top of any connection-wide ``message_spacing``.

        Keyed by unit id: the gap belongs to the unit, not the handle, so it
        applies to every request to this unit on the connection — including ones
        issued through other handles the owner hands out for the same id. Use it
        for a single slow device on a link that other units share without needing
        the pause. ``0`` clears it.
        """

    def on_connection_lost(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a callback fired when the link drops; returns an unsubscribe."""
