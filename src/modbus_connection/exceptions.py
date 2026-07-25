"""Define the public Modbus exception hierarchy."""


class ModbusError(Exception):
    """Base class for every error raised by a modbus_connection backend."""


class ModbusConnectionError(ModbusError):
    """The link is down: not connected, connection lost, or transport failure."""


class ClientClosedError(ModbusConnectionError):
    """A request was attempted on a closed connection."""


class ModbusTimeoutError(ModbusError, TimeoutError):
    """A Modbus operation timed out."""


class ModbusProtocolError(ModbusError):
    """A response was not a valid Modbus frame."""


class ModbusExceptionError(ModbusError):
    """The device returned a Modbus exception response."""

    def __init__(self, exception_code: int | None, message: str | None = None) -> None:
        self.exception_code = exception_code
        super().__init__(
            message or f"Device returned Modbus exception code {exception_code}"
        )


class BlockReadError(ModbusExceptionError):
    """A device rejected one block of a component read."""

    def __init__(
        self, space: str, address: int, count: int, exception_code: int | None
    ) -> None:
        self.space = space
        self.address = address
        self.count = count
        super().__init__(
            exception_code,
            f"{space} block read at address {address} (count {count}) "
            f"returned Modbus exception code {exception_code}",
        )
