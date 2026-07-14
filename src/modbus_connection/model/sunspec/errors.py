"""SunSpec error types."""


class SunSpecError(Exception):
    """Raised when a device does not behave like a SunSpec device."""


class SunSpecMapShiftError(SunSpecError):
    """The model header no longer matches its discovered location.

    The device shifted its register map - a configuration change resized a
    model - so every component built from the old scan reads stale
    addresses. Re-scan and build new components.
    """
