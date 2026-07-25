"""Write component fields."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..decode import decode_int

if TYPE_CHECKING:
    from .._protocol import ModbusUnit
    from ._planning import RegisterSpace
    from .fields import RegisterField, _BitField


async def write_register_field(
    unit: ModbusUnit,
    field: RegisterField[Any],
    address: int,
    space: RegisterSpace,
    value: Any,
    *,
    label: str,
    scale_address: int | None = None,
) -> None:
    """Write ``value`` to a register field at ``address``.

    Raises ``AttributeError`` if the field is read-only and ``ValueError`` if
    the value cannot be scaled.
    """
    if not field.writable:
        raise AttributeError(f"{label} is read-only")
    if space != "holding":
        raise AttributeError(
            f"{label} is in the {space} register space, which is read-only"
        )
    if callable(field.writable):
        # The validator vets/coerces the value and returns what to write,
        # or raises to reject it.
        value = field.writable(value)
    scale_exponent = None
    if field.scale_register is not None and scale_address is not None:
        (word,) = await unit.read_holding_registers(scale_address, 1)
        scale_exponent = decode_int([word], signed=True)
    words = field.encode(value, scale_exponent)
    if field.force_fc16 or len(words) > 1:
        await unit.write_registers(address, words)
    else:
        await unit.write_register(address, words[0])


async def write_bit_field(
    unit: ModbusUnit,
    field: _BitField,
    address: int,
    value: Any,
    *,
    label: str,
) -> None:
    """Write ``value`` to a bit field.

    Raises ``AttributeError`` if the field is read-only.
    """
    if not field.writable:
        raise AttributeError(f"{label} is read-only")
    if callable(field.writable):
        value = field.writable(value)
    await unit.write_coil(address, bool(value))
