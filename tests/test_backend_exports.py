"""Backend-local connection class exports."""

from __future__ import annotations

import inspect
from collections.abc import Callable

import pytest

import modbus_connection.pymodbus as pymodbus_backend
import modbus_connection.tmodbus as tmodbus_backend


@pytest.mark.parametrize(
    ("backend", "legacy_name"),
    [
        pytest.param(pymodbus_backend, "PymodbusConnection", id="pymodbus"),
        pytest.param(tmodbus_backend, "TmodbusConnection", id="tmodbus"),
    ],
)
def test_modbus_connection_is_canonical_export(
    backend: object, legacy_name: str
) -> None:
    canonical = backend.ModbusConnection  # type: ignore[attr-defined]
    legacy = getattr(backend, legacy_name)

    assert canonical is legacy
    assert canonical.__name__ == "ModbusConnection"
    assert "ModbusConnection" in backend.__all__  # type: ignore[attr-defined]
    assert legacy_name in backend.__all__  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "factory",
    [
        pymodbus_backend.connect_tcp,
        pymodbus_backend.connect_udp,
        pymodbus_backend.connect_tls,
        pymodbus_backend.connect_serial,
        tmodbus_backend.connect_tcp,
        tmodbus_backend.connect_udp,
        tmodbus_backend.connect_tls,
        tmodbus_backend.connect_serial,
    ],
)
def test_factory_return_annotation_is_backend_modbus_connection(
    factory: Callable[..., object],
) -> None:
    assert (
        inspect.get_annotations(factory, eval_str=True)["return"]
        is factory.__globals__["ModbusConnection"]
    )
