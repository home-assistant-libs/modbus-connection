"""Read a real IEEE 1547 device's registers through the generated classes.

The generator bakes ``--count`` values into the classes it emits, and places
and types every point from the model definition. The rest of the suite builds
its fixtures from the same arithmetic the generator uses, so it can only ever
agree with itself; these cases check it against a device instead.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from modbus_connection.cli_helper import group_rows
from modbus_connection.mock import MockModbusConnection
from modbus_connection.model import Component
from modbus_connection.model.sunspec import SunSpecModel, scan
from modbus_connection.model.sunspec.generate import _camel, _snake, generate_source

# A scan of a FranklinWH aGate X, which implements the whole IEEE 1547 profile
# (models 1 and 701-715): a holding-register dump plus, per model, where the
# device puts each of its points and what it reads there. It is the only check
# that a generated layout describes a device rather than just agreeing with the
# arithmetic that produced it.
AGATE_LAYOUT = Path(__file__).parent / "fixtures" / "agate_sunspec_layout.json"
AGATE = json.loads(AGATE_LAYOUT.read_text())


def _agate_unit() -> Any:
    """A mock unit serving the device's registers."""
    dump = AGATE["holding"]
    words = dump["words"]
    unit = MockModbusConnection().for_unit(1)
    unit.load_raw(
        {
            "holding": {
                dump["start"] + index: int(words[at : at + 4], 16)
                for index, at in enumerate(range(0, len(words), 4))
            }
        }
    )
    return unit


# A `pad` reserves registers and generates no field, so the device names it but
# nothing reads it.
_UNPLACED_TYPES = frozenset({"pad"})

# Where the device and the model definition genuinely disagree, so the value
# cannot decode to what the device shows. Model 502's Stat enum starts at
# OFF = 1 and has no symbol for the 0 this device reports, which is the
# documented unknown-code path: warn once, decode None.
AGATE_UNDECODABLE = {(502, "Stat")}


def _scale_references(group: dict[str, Any]) -> list[tuple[str, str]]:
    """Every ``(point name, scale factor name)`` pair, at any nesting."""
    pairs = [
        (point["name"], point["sf"])
        for point in group.get("points", [])
        if isinstance(point.get("sf"), str)
    ]
    for sub in group.get("groups", []):
        pairs += _scale_references(sub)
    return pairs


def _fields_named(component: Component, attr: str) -> list[Any]:
    """Every resolved field under this component with that attribute name."""
    found = (
        [component.resolved_fields[attr]] if attr in component.resolved_fields else []
    )
    for _name, instances in group_rows(component):
        for instance in instances:
            found += _fields_named(instance, attr)
    return found


def _attribute_for(point_name: str, resolved: Mapping[str, Any]) -> str | None:
    """The generated attribute for a SunSpec point name, trailing _ and all."""
    attr = _snake(point_name)
    while attr and attr not in resolved:
        # attr_name appends _ to dodge a reserved or already-claimed name.
        attr = f"{attr}_" if len(attr) < len(point_name) + 8 else ""
    return attr or None


@pytest.fixture(scope="session")
def agate_module(official_models: Path) -> dict[str, Any]:
    """One generated module for the whole device, as the CLI would emit it.

    The generator has no per-device container - it emits a class per model - so
    a device library generates every model it found into one module. Doing the
    same here also covers the models sharing that module: their enums fold
    together and their class names must not collide.
    """
    definitions = [
        json.loads((official_models / f"model_{entry['id']}.json").read_text())
        for entry in AGATE["models"]
    ]
    counts = {entry["id"]: entry["counts"] for entry in AGATE["models"]}
    namespace: dict[str, Any] = {}
    source = generate_source(definitions, counts)
    exec(compile(source, "agate_models", "exec"), namespace)  # noqa: S102
    return namespace


@pytest.mark.parametrize(
    "entry", AGATE["models"], ids=lambda entry: f"{entry['id']}-{entry['name']}"
)
async def test_generated_layout_matches_a_real_device(
    official_models: Path, agate_module: dict[str, Any], entry: dict[str, Any]
) -> None:
    """Every point the device reports sits where the generated layout puts it.

    The device's scan is the ground truth: its header addresses, its lengths and
    the address it reports for each point all come off the wire, so a layout
    generated from its counts has to agree with them point for point.
    """
    model_json = json.loads((official_models / f"model_{entry['id']}.json").read_text())
    # The generator names the class after the model's group, which is the name
    # the scan records: 'DERVoltVar', 'solar_module' -> SolarModule.
    model_cls = agate_module[_camel(entry["name"])]

    model = SunSpecModel(
        model_id=entry["id"], address=entry["address"], length=entry["length"]
    )
    component = model_cls(_agate_unit(), model)
    await component.async_update()

    # Each point the device names, at the address it names it. Paired by name,
    # not just matched as a set of addresses: two points swapped between two
    # addresses has to fail.
    resolved = component.resolved_fields
    device_address = {name: address for name, address, _type, _value in entry["points"]}
    references = _scale_references(model_json["group"])
    referenced_sf = {sf for _point, sf in references}

    checked = 0
    for name, address, kind, shown in entry["points"]:
        if name in ("ID", "L") or kind in _UNPLACED_TYPES:
            continue
        checked += 1
        if kind == "sunssf" and name in referenced_sf:
            # Folded into the fields that reference it rather than emitted; the
            # pairing is checked below, against each referencing point.
            continue
        attr = _attribute_for(name, resolved)
        assert attr is not None, f"model {entry['id']}: no field generated for {name}"
        assert resolved[attr].address == address, (
            f"model {entry['id']}: {name} is read at {resolved[attr].address},"
            f" the device reports it at {address}"
        )
        # And reading the dump back through the field returns what the device
        # showed there, so the generated type and width match the device's too.
        decoded = getattr(component, attr)
        expected = None if (entry["id"], name) in AGATE_UNDECODABLE else shown
        if expected is None or isinstance(expected, str):
            assert decoded == expected, (
                f"model {entry['id']}: {name} decodes to {decoded!r},"
                f" the device reports {expected!r}"
            )
        else:
            assert decoded == pytest.approx(expected), (
                f"model {entry['id']}: {name} decodes to {decoded!r},"
                f" the device reports {expected!r}"
            )
    assert checked == sum(
        1
        for name, _address, kind, _value in entry["points"]
        if name not in ("ID", "L") and kind not in _UNPLACED_TYPES
    )

    # Each scaled point reads the scale register the device puts that scale
    # factor at - a point wired to the wrong one of a model's several scale
    # factors has to fail, so this pairs them rather than matching addresses.
    for point_name, sf_name in references:
        if sf_name not in device_address:
            continue  # a scale factor inside a repeated block the scan skips
        for field in _fields_named(component, _snake(point_name)):
            assert field.scale_address == device_address[sf_name], (
                f"model {entry['id']}: {point_name} reads its scale factor at"
                f" {field.scale_address}, but the device puts {sf_name} at"
                f" {device_address[sf_name]}"
            )


async def test_scan_walks_a_real_device_chain() -> None:
    """Scanning the device's own registers finds its models where they sit."""
    found = await scan(_agate_unit(), AGATE["holding"]["start"])
    assert [(model.model_id, model.address, model.length) for model in found.chain] == [
        (entry["id"], entry["address"], entry["length"]) for entry in AGATE["models"]
    ]
    # The chain tiles the map: each header one span after the one before.
    for model, following in zip(found.chain, found.chain[1:], strict=False):
        assert model.address + model.span == following.address
