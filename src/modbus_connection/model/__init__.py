"""A small device-modelling framework over the ``ModbusUnit`` protocol.

Map a device's registers and coils to typed Python attributes, then read the
whole device (or one sub-system) in as few Modbus calls as possible. It is
backend-neutral: it talks only to a ``ModbusUnit``, so it runs over pymodbus,
tmodbus, or the in-memory mock.

A ``Component`` is a sub-system whose attributes are ``RegisterField`` /
``CoilField`` descriptors (usually built with the typed factories below)::

    from modbus_connection.model import Component, gauge, integer, coil

    class Meter(Component):
        voltage = gauge(0, 0.1, unit="V")
        current = gauge(1, 0.1, unit="A")
        energy = uint32(2, unit="Wh")
        relay = coil(0, writable=True)

    meter = Meter(unit)
    await meter.async_update()
    meter.voltage            # float | None

Generic field types ship here: scaled / unscaled integers (16/32/64-bit), raw
words, ``float32`` / ``float64``, strings, ``enum`` / ``flags`` fields that map
natively to an ``IntEnum`` / ``IntFlag``, and the bit fields ``coil`` (FC01) /
``discrete_input`` (FC02).
The SunSpec module :mod:`modbus_connection.model.sunspec` adds the same types
pre-wired with their per-point "unimplemented" sentinels, plus the address types
(``ipaddr`` / ``ipv6addr`` / ``eui48``).

Shaping that neither covers — composing or transforming a value, packed
dates/times, sentinel handling beyond a single ``nan`` — belongs in the consumer,
done with a private field plus a normal ``@property`` so static typing stays
exact. For example, presenting a version register prefixed with a hard-coded
model name::

    from modbus_connection.model import Component, string

    class Controller(Component):
        _firmware = string(10, 4)  # 4 registers of ASCII, e.g. "1.23"

        @property
        def model(self) -> str | None:
            firmware = self._firmware
            return f"TROVIS 5576 ({firmware})" if firmware is not None else None

Reads are pooled into block reads. A device may pass its readable address
``ranges`` so the planner merges only within a range and never reads across an
unreadable gap. Register fields default to the holding space (FC03); a component
that sets ``register_space = "input"`` is read with FC04 instead, and the two
spaces are always planned and read separately. Bits work the same way over their
own pair of spaces: ``coil`` fields are read/written via FC01 and
``discrete_input`` fields are read from FC02 (read-only); a component may mix the
two and they are planned and read separately.

The implementation is split across :mod:`~modbus_connection.model.fields` (the
field descriptors and factories), :mod:`~modbus_connection.model.component` and
:mod:`~modbus_connection.model.component_group`; everything public is re-exported
here.
"""

from __future__ import annotations

from .._types import BitSpace
from ._planning import Range, RegisterSpace
from .component import Component, UpdateListener
from .component_group import ComponentGroup
from .fields import (
    CoilField,
    DiscreteInputField,
    RegisterField,
    WriteValidator,
    coil,
    discrete_input,
    enum,
    flags,
    float32,
    float64,
    gauge,
    int32,
    int64,
    integer,
    raw_register,
    string,
    uint32,
    uint64,
)

__all__ = [
    "BitSpace",
    "CoilField",
    "Component",
    "ComponentGroup",
    "DiscreteInputField",
    "Range",
    "RegisterField",
    "RegisterSpace",
    "UpdateListener",
    "WriteValidator",
    "coil",
    "discrete_input",
    "enum",
    "flags",
    "float32",
    "float64",
    "gauge",
    "int32",
    "int64",
    "integer",
    "raw_register",
    "string",
    "uint32",
    "uint64",
]
