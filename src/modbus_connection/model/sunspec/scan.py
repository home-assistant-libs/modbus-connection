"""SunSpec model-chain scanning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from ...decode import decode_uint32
from .errors import SunSpecError

if TYPE_CHECKING:
    from ..._protocol import ModbusUnit

_SUNSPEC_MARKER: Final = 0x53756E53  # "SunS"
_END_MODEL_ID: Final = 0xFFFF
# Sanity limit against malformed maps sending the chain walk astray.
_MAX_MODELS: Final = 100


@dataclass(frozen=True)
class SunSpecModel:
    """Locate a SunSpec model in the register map."""

    model_id: int
    address: int
    length: int


async def scan(unit: ModbusUnit, base_address: int) -> dict[int, list[SunSpecModel]]:
    """Return the discovered SunSpec models by ID.

    Raises ``SunSpecError`` if the marker is absent or the chain is invalid.
    """
    marker = await unit.read_holding_registers(base_address, 2)
    if decode_uint32(marker) != _SUNSPEC_MARKER:
        raise SunSpecError(f"No SunSpec marker found at register {base_address}")

    models: dict[int, list[SunSpecModel]] = {}
    address = base_address + 2
    for _ in range(_MAX_MODELS):
        model_id, length = await unit.read_holding_registers(address, 2)
        if model_id == _END_MODEL_ID:
            return models
        models.setdefault(model_id, []).append(
            SunSpecModel(model_id=model_id, address=address, length=length)
        )
        address += 2 + length
    raise SunSpecError(f"Model chain not terminated after {_MAX_MODELS} models")
