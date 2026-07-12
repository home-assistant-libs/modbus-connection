"""Exceptions raised by modbus_connection backends.

These are backend-neutral: both the pymodbus and tmodbus implementations map
their library-specific errors onto this small hierarchy, so consumers catch the
same types regardless of which backend is in use.
"""


class ModbusError(Exception):
    """Base class for every error raised by a modbus_connection backend."""


class ModbusConnectionError(ModbusError):
    """The link is down: not connected, connection lost, or transport failure."""


class ModbusTimeoutError(ModbusError, TimeoutError):
    """An operation timed out.

    Either a request was sent and no (valid) response arrived in time, or a
    connect attempt did not complete in time. Also a builtin ``TimeoutError``
    (and therefore an ``OSError``), so ``except TimeoutError`` catches it too.
    """


class ModbusProtocolError(ModbusError):
    """A response arrived but was not a valid frame (bad CRC/LRC, framing, header).

    Distinct from ``ModbusTimeoutError`` (no response arrived) and
    ``ModbusExceptionError`` (a valid error PDU from the device).
    """


class ModbusExceptionError(ModbusError):
    """The device answered with a Modbus exception response (a valid error PDU).

    ``exception_code`` is the raw Modbus exception code (1 = illegal function,
    2 = illegal data address, 3 = illegal data value, ...). It is ``None`` only
    when the backend could not decode a specific code.
    """

    def __init__(self, exception_code: int | None, message: str | None = None) -> None:
        self.exception_code = exception_code
        super().__init__(
            message or f"Device returned Modbus exception code {exception_code}"
        )


class BlockReadError(ModbusExceptionError):
    """A device exception response on one block of a component's pooled read.

    Enriches :class:`ModbusExceptionError` with the block that failed: ``space`` is
    the register/bit space it was read from, ``address`` the first address of the
    block, and ``count`` how many addresses it covered. The inherited
    ``exception_code`` is why the device refused the read; the original
    :class:`ModbusExceptionError` is chained as ``__cause__``.
    """

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
