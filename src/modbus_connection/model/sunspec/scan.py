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
    """Location of a SunSpec model in the register map.

    ``address`` points at the 2-register model header (model ID, length);
    ``length`` is the number of data registers following the header.
    """

    model_id: int
    address: int
    length: int


async def scan(unit: ModbusUnit, base_address: int) -> dict[int, list[SunSpecModel]]:
    """Walk the SunSpec model chain and return the discovered models by ID.

    ``base_address`` is the 0-based register address of the map's ``"SunS"``
    marker - the SunSpec spec sanctions 0, 40000 and 50000, and an
    integration knows which one its manufacturer uses. Raises
    :class:`SunSpecError` when the marker is missing or the chain doesn't
    terminate.

    The same model ID can occur more than once in a chain (e.g. several
    meters), so each ID maps to its occurrences in chain order.
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
