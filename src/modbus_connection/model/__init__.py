"""Map Modbus values to typed device components."""

from __future__ import annotations

from .._types import BitSpace
from ._component_base import UpdateListener
from ._const import Range, RegisterSpace
from .component import (
    Component,
    RepeatingGroupField,
    repeating_group,
)
from .component_group import ComponentGroup
from .fields import (
    BitField,
    CoilField,
    Converter,
    DiscreteInputField,
    FloatField,
    NumberField,
    RawField,
    RegisterField,
    StringField,
    WriteValidator,
    boolean,
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
    "BitField",
    "BitSpace",
    "CoilField",
    "Component",
    "ComponentGroup",
    "Converter",
    "DiscreteInputField",
    "FloatField",
    "ManualComponent",
    "NumberField",
    "Range",
    "RawField",
    "RegisterField",
    "RegisterSpace",
    "RepeatingGroupField",
    "StringField",
    "UpdateListener",
    "WriteValidator",
    "boolean",
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
