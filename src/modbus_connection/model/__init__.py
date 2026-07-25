"""Map Modbus values to typed device components."""

from __future__ import annotations

from .._types import BitSpace
from ._component_base import UpdateListener
from ._planning import Range, RegisterSpace
from .component import (
    Component,
    RepeatingGroupField,
    repeating_group,
)
from .component_group import ComponentGroup
from .fields import (
    CoilField,
    Converter,
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
from .manual import ManualComponent

__all__ = [
    "BitSpace",
    "CoilField",
    "Component",
    "ComponentGroup",
    "Converter",
    "DiscreteInputField",
    "ManualComponent",
    "Range",
    "RegisterField",
    "RegisterSpace",
    "RepeatingGroupField",
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
    "repeating_group",
    "string",
    "uint32",
    "uint64",
]
