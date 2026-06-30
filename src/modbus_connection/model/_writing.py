"""Shared field-write helpers for Component and ManualComponent.

The two differ only in how they resolve a field's address and space (a Component
applies its ``index``/``stride``; a ManualComponent uses absolute addresses), so
the actual write — read-only checks, the ``writable`` validator, and the
FC06/FC16 choice — lives here once and both call into it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
) -> None:
    """Write ``value`` to a register field at ``address``.

    Raises if the field is read-only or in a non-holding (read-only) space. A
    ``writable`` validator callable vets/coerces ``value`` first. The write uses
    FC06 (single) / FC16 (multiple), or FC16 even for one register when the field
    sets ``force_fc16``. ``label`` names the field in error messages.
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
    words = field.encode(value)
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
    """Write ``value`` to a bit field at ``address`` (FC05).

    Raises if the bit is read-only (a discrete input always is — its ``writable``
    is ``False``). A ``writable`` validator callable vets/coerces ``value`` first.
    ``label`` names the field in error messages.
    """
    if not field.writable:
        raise AttributeError(f"{label} is read-only")
    if callable(field.writable):
        value = field.writable(value)
    await unit.write_coil(address, bool(value))
