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
    """A response arrived but was not a valid Modbus frame.

    A bad CRC/LRC, a framing error, or a header that does not match the request:
    something came back over the wire, but it could not be parsed as the expected
    reply. Distinct from ``ModbusTimeoutError`` (no response arrived at all) and
    from ``ModbusExceptionError`` (a *valid* error PDU from the device).

    Backends that cannot tell a garbled response apart from a missing one (e.g.
    pymodbus, which raises a single ``ModbusIOException`` for both) surface these
    as ``ModbusTimeoutError`` instead; only backends that distinguish the two
    (e.g. tmodbus) raise this type.
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
