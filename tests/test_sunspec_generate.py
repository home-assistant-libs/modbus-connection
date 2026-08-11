"""Tests for the SunSpec model generator (modbus_connection.model.sunspec.generate)."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from modbus_connection.mock import MockModbusConnection
from modbus_connection.model.sunspec import SunSpecModel
from modbus_connection.model.sunspec.generate import (
    SunSpecGenerationError,
    _member,
    _snake,
    generate_source,
    main,
)

# A vendor-range model exercising every generated construct: header skip,
# dynamic and static scale factors, writability, strings, pads, enums,
# bitfields, a count point and a repeating block scaled from the fixed block.
MODEL_JSON: dict[str, Any] = {
    "id": 64111,
    "group": {
        "label": "Test Model",
        "name": "test",
        "type": "group",
        "points": [
            {"name": "ID", "type": "uint16", "size": 1, "mandatory": "M"},
            {"name": "L", "type": "uint16", "size": 1, "mandatory": "M"},
            {"name": "A", "type": "uint16", "size": 1, "sf": "A_SF", "units": "A"},
            {"name": "A_SF", "type": "sunssf", "size": 1},
            {"name": "Offs", "type": "int16", "size": 1, "sf": -1},
            {"name": "DA", "type": "uint16", "size": 1, "access": "RW"},
            {"name": "Mn", "type": "string", "size": 2, "label": "Manufacturer"},
            {"name": "Pad", "type": "pad", "size": 1},
            {
                "name": "St",
                "type": "enum16",
                "size": 1,
                "symbols": [
                    {"name": "ON", "value": 1},
                    {"name": "OFF", "value": 2},
                ],
            },
            {
                "name": "Evt",
                "type": "bitfield16",
                "size": 1,
                "symbols": [
                    {"name": "FAULT", "value": 0},
                    {"name": "WARN", "value": 3},
                ],
            },
            {"name": "N", "type": "count", "size": 1},
            {"name": "Wh", "type": "acc32", "size": 2, "sf": "A_SF", "units": "Wh"},
        ],
        "groups": [
            {
                "name": "module",
                "type": "group",
                "count": 0,
                "points": [
                    {
                        "name": "V",
                        "type": "uint16",
                        "size": 1,
                        "sf": "A_SF",
                        "units": "V",
                    },
                    {"name": "Lbl", "type": "string", "size": 2},
                ],
            }
        ],
    },
}


def test_name_conversion() -> None:
    assert _snake("AphA") == "aph_a"
    assert _snake("PPVphAB") == "pp_vph_ab"
    assert _snake("DCA_SF") == "dca_sf"
    assert _snake("WH") == "wh"
    assert _snake("class") == "class_"
    assert _member("1PH") == "V_1PH"
    assert _member("GROUND-FAULT") == "GROUND_FAULT"


def test_generated_layout() -> None:
    source = generate_source([MODEL_JSON])
    # The header points are SunSpecComponent's model_id/model_length,
    # so ID and L generate no fields of their own.
    assert "model_id" not in source
    assert "uint16(0)" not in source
    assert "uint16(1)" not in source
    # Points at their model-relative addresses, with sf / units / access wired.
    assert "a = uint16(2, scale_register=3, unit='A')" in source
    # A_SF is referenced by A/Wh, so it is not emitted as its own field - the
    # planner reads register 3 for those points regardless.
    assert "sunssf(3)" not in source
    assert "a_sf" not in source
    assert "offs = int16(4, scale=0.1)" in source
    assert "da = uint16(5, writable=True)" in source
    # The label (and desc, when present) becomes an attribute docstring.
    assert 'mn = string(6, 2)\n    """Manufacturer."""' in source
    assert "st = enum16(9, St)" in source
    assert "evt = bitfield16(10, Evt)" in source
    assert "n = uint16(11)" in source
    assert "wh = acc32(12, scale_register=3, unit='Wh')" in source
    # Bitfield symbol values are bit positions; enums are plain values.
    assert "ON = 1" in source
    assert "WARN = 1 << 3" in source
    # Pads produce no field.
    assert "pad" not in source
    # The repeating block: its own Component class at instance-0 addresses,
    # sized by the count point, scaled from the shared fixed block.
    assert "class TestModule(Component):" in source
    assert "v = uint16(14, scale_register=3, unit='V')" in source
    assert "lbl = string(15, 2)" in source
    assert "module = repeating_group(uint16(11), TestModule, stride=3)" in source


async def test_generated_module_decodes() -> None:
    source = generate_source([MODEL_JSON])
    namespace: dict[str, Any] = {}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102
    model_cls = namespace["Test"]

    base = 40002
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update(
        {
            base: 64111,  # model ID
            base + 1: 18,  # model length (12 fixed + 2 modules * 3)
            base + 2: 1234,  # A
            base + 3: (-2) & 0xFFFF,  # A_SF = -2
            base + 4: 25,  # Offs, static scale 0.1
            base + 5: 7,  # DA
            base + 6: 0x4142,  # Mn = "AB"
            base + 7: 0x0000,
            base + 8: 0xBEEF,  # Pad
            base + 9: 1,  # St = ON
            base + 10: 0b1001,  # Evt = FAULT | WARN
            base + 11: 2,  # N = 2 modules
            base + 12: 0x0001,  # Wh = 100000, scaled by A_SF
            base + 13: 0x86A0,
            base + 14: 100,  # module[0].V
            base + 15: 0x4D31,  # module[0].Lbl = "M1"
            base + 16: 0x0000,
            base + 17: 200,  # module[1].V
            base + 18: 0x4D32,  # module[1].Lbl = "M2"
            base + 19: 0x0000,
        }
    )
    component = model_cls(unit, SunSpecModel(model_id=64111, address=base, length=18))
    await component.async_update()

    assert component.a == pytest.approx(12.34)  # scaled by the read A_SF
    assert component.offs == pytest.approx(2.5)
    assert component.da == 7
    assert component.mn == "AB"
    assert component.st == namespace["St"].ON
    assert component.evt == namespace["Evt"].FAULT | namespace["Evt"].WARN
    assert component.n == 2
    assert component.wh == pytest.approx(1000.0)

    modules = component.module
    assert len(modules) == 2
    # Instance addresses shift by the stride; the shared scale factor doesn't.
    assert modules[0].v == pytest.approx(1.0)
    assert modules[0].lbl == "M1"
    assert modules[1].v == pytest.approx(2.0)
    assert modules[1].lbl == "M2"


def test_count_zero_without_count_point_generates_hint() -> None:
    model = copy.deepcopy(MODEL_JSON)
    model["group"]["points"] = [p for p in model["group"]["points"] if p["name"] != "N"]
    source = generate_source([model])
    assert "# module = repeating_group(N, TestModule, stride=3)" in source


def test_string_count_reference() -> None:
    model = copy.deepcopy(MODEL_JSON)
    model["group"]["groups"][0]["count"] = "N"
    source = generate_source([model])
    assert "module = repeating_group(uint16(11), TestModule, stride=3)" in source


def test_fixed_count_folds_into_layout() -> None:
    model = copy.deepcopy(MODEL_JSON)
    model["group"]["groups"][0]["count"] = 3
    source = generate_source([model])
    assert "module = repeating_group(3, TestModule, stride=3)" in source


def test_unreferenced_scale_factor_is_kept() -> None:
    # A sunssf no point references is the only way to read that register, so
    # it keeps its field even though referenced ones are dropped.
    model = copy.deepcopy(MODEL_JSON)
    model["group"]["points"].insert(
        -1, {"name": "Spare_SF", "type": "sunssf", "size": 1}
    )
    source = generate_source([model])
    assert "spare_sf = sunssf(12)" in source  # kept: nothing references it
    assert "a_sf" not in source  # dropped: referenced by A


def test_nested_fixed_count_group_wires_statically() -> None:
    model = copy.deepcopy(MODEL_JSON)
    model["group"]["groups"][0]["groups"] = [
        {
            "name": "chan",
            "count": 2,
            "points": [{"name": "Val", "type": "uint16", "size": 1}],
        }
    ]
    source = generate_source([model])
    # The nested block folds into the layout: its class sits at instance-0
    # addresses and the enclosing stride grows by count * size.
    assert "class TestModuleChan(Component):" in source
    assert "val = uint16(17)" in source
    assert "chan = repeating_group(2, TestModuleChan, stride=1)" in source
    assert "module = repeating_group(uint16(11), TestModule, stride=5)" in source


async def test_nested_fixed_count_group_decodes() -> None:
    model = copy.deepcopy(MODEL_JSON)
    model["group"]["groups"][0]["groups"] = [
        {
            "name": "chan",
            "count": 2,
            "points": [{"name": "Val", "type": "uint16", "size": 1}],
        }
    ]
    namespace: dict[str, Any] = {}
    exec(  # noqa: S102
        compile(generate_source([model]), "<generated>", "exec"), namespace
    )
    base = 100
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update(
        {
            base: 64111,
            base + 1: 22,  # 12 fixed + 2 modules * (3 + 2 * 1)
            base + 3: (-2) & 0xFFFF,  # A_SF
            base + 11: 2,  # N = 2 modules
            # module[0] at base+14, module[1] at base+19 (stride 5)
            base + 14: 100,
            base + 17: 11,
            base + 18: 12,
            base + 19: 200,
            base + 22: 21,
            base + 23: 22,
        }
    )
    component = namespace["Test"](
        unit, SunSpecModel(model_id=64111, address=base, length=22)
    )
    await component.async_update()
    modules = component.module
    assert [m.v for m in modules] == [pytest.approx(1.0), pytest.approx(2.0)]
    assert [c.val for c in modules[0].chan] == [11, 12]
    assert [c.val for c in modules[1].chan] == [21, 22]


NESTED_MODEL_JSON: dict[str, Any] = {
    # The 7xx shape (e.g. model 705): device-sized curves of device-sized
    # points, both counts in the top block.
    "id": 64222,
    "group": {
        "label": "Curves",
        "name": "curves",
        "points": [
            {"name": "ID", "type": "uint16", "size": 1},
            {"name": "L", "type": "uint16", "size": 1},
            {"name": "NPt", "type": "uint16", "size": 1},
            {"name": "NCrv", "type": "uint16", "size": 1},
        ],
        "groups": [
            {
                "name": "crv",
                "count": "NCrv",
                "points": [{"name": "ActPt", "type": "uint16", "size": 1}],
                "groups": [
                    {
                        "name": "pt",
                        "count": "NPt",
                        "points": [{"name": "V", "type": "uint16", "size": 1}],
                    }
                ],
            }
        ],
    },
}


def test_device_sized_nested_blocks_generate_classes_and_hints() -> None:
    source = generate_source([NESTED_MODEL_JSON])
    # Classes for every level, at instance-0 addresses.
    assert "class CurvesCrvPt(Component):" in source
    assert "v = uint16(5)" in source
    assert "class CurvesCrv(Component):" in source
    assert "act_pt = uint16(4)" in source
    # Neither block can be wired without its count, and the hint names the
    # option that supplies it — the curve's stride depends on NPt too.
    assert "# Re-run with --count 64222:NPt=<n> to emit it:" in source
    assert "# pt = repeating_group(<n>, CurvesCrvPt, stride=1)" in source
    assert (
        "# Re-run with --count 64222:NCrv=<n> --count 64222:NPt=<n> to emit it:"
        in source
    )
    assert "# crv = repeating_group(<n>, CurvesCrv, stride=<...>)" in source


def test_counts_wire_device_sized_nested_blocks() -> None:
    source = generate_source([NESTED_MODEL_JSON], {64222: {"NPt": 4, "NCrv": 3}})
    assert "pt = repeating_group(4, CurvesCrvPt, stride=1)" in source
    # The curve's stride is 1 own point + 4 points of 1 register.
    assert "crv = repeating_group(3, CurvesCrv, stride=5)" in source


async def test_counted_nested_blocks_decode() -> None:
    """The generated fixed-count classes read a curve model off the wire."""
    namespace: dict[str, Any] = {}
    source = generate_source([NESTED_MODEL_JSON], {64222: {"NPt": 2, "NCrv": 2}})
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102
    base = 100
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update(
        {
            base: 64222,
            base + 1: 10,  # 4 fixed + 2 curves * (1 + 2 * 1)
            base + 2: 2,  # NPt
            base + 3: 2,  # NCrv
            base + 4: 11,  # curve 0: ActPt, then its two points
            base + 5: 12,
            base + 6: 13,
            base + 7: 21,  # curve 1
            base + 8: 22,
            base + 9: 23,
        }
    )
    component = namespace["Curves"](
        unit, SunSpecModel(model_id=64222, address=base, length=10)
    )
    await component.async_update()
    curves = component.crv
    assert [c.act_pt for c in curves] == [11, 21]
    assert [[p.v for p in c.pt] for c in curves] == [[12, 13], [22, 23]]


def test_counts_place_a_block_that_another_follows() -> None:
    """A counted block has a known size, so later blocks are placeable."""
    model = copy.deepcopy(NESTED_MODEL_JSON)
    model["group"]["groups"].append(
        {"name": "after", "points": [{"name": "X", "type": "uint16", "size": 1}]}
    )
    source = generate_source([model], {64222: {"NPt": 4, "NCrv": 3}})
    assert "x = uint16(19)" in source  # 4 fixed + 3 curves * 5 registers


def test_cli_count_option(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    model = tmp_path / "model_64222.json"
    model.write_text(json.dumps(NESTED_MODEL_JSON))
    assert main([str(model), "--count", "64222:NPt=4", "--count", "64222:NCrv=3"]) == 0
    assert "crv = repeating_group(3, CurvesCrv, stride=5)" in capsys.readouterr().out


def test_cli_rejects_a_malformed_count(tmp_path: Path) -> None:
    model = tmp_path / "model_64222.json"
    model.write_text(json.dumps(NESTED_MODEL_JSON))
    with pytest.raises(SystemExit):
        main([str(model), "--count", "NPt=4"])


def test_count_for_an_unrepeated_point_is_rejected() -> None:
    with pytest.raises(SunSpecGenerationError, match="no group is repeated by Nope"):
        generate_source([NESTED_MODEL_JSON], {64222: {"Nope": 2}})


def test_device_sized_block_must_be_last() -> None:
    model = copy.deepcopy(NESTED_MODEL_JSON)
    model["group"]["groups"].append(
        {"name": "after", "points": [{"name": "X", "type": "uint16", "size": 1}]}
    )
    with pytest.raises(SunSpecGenerationError, match="not the last block"):
        generate_source([model])


def test_unknown_count_reference_is_rejected() -> None:
    model = copy.deepcopy(MODEL_JSON)
    model["group"]["groups"][0]["count"] = "Nope"
    with pytest.raises(SunSpecGenerationError, match="not defined in the model"):
        generate_source([model])


def _with_in_block_scale(model: dict[str, Any]) -> dict[str, Any]:
    """Give the module block its own scale factor: V scaled by an inner V_SF."""
    model = copy.deepcopy(model)
    module = model["group"]["groups"][0]
    module["points"][0]["sf"] = "V_SF"
    module["points"].append({"name": "V_SF", "type": "sunssf", "size": 1})
    return model


def test_in_block_scale_factor_sets_scale_in_block() -> None:
    source = generate_source([_with_in_block_scale(MODEL_JSON)])
    assert "scale_in_block = True" in source
    # V's scale register is the block's own V_SF, at its instance-0 address.
    assert "v = uint16(14, scale_register=17, unit='V')" in source
    # V_SF is referenced, so no field of its own - but it still occupies a
    # register, so the block stride includes it.
    assert "sunssf" not in source
    assert "module = repeating_group(uint16(11), TestModule, stride=4)" in source


async def test_in_block_scale_factor_decodes_per_instance() -> None:
    namespace: dict[str, Any] = {}
    exec(  # noqa: S102
        compile(
            generate_source([_with_in_block_scale(MODEL_JSON)]),
            "<generated>",
            "exec",
        ),
        namespace,
    )
    base = 200
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update(
        {
            base: 64111,
            base + 1: 20,  # 12 fixed + 2 modules * 4
            base + 11: 2,  # N = 2 modules
            # Same raw V, but each instance carries its own scale factor.
            base + 14: 100,
            base + 17: (-1) & 0xFFFF,  # module[0]: V_SF = -1
            base + 18: 100,
            base + 21: 1,  # module[1]: V_SF = 1
        }
    )
    component = namespace["Test"](
        unit, SunSpecModel(model_id=64111, address=base, length=20)
    )
    await component.async_update()
    modules = component.module
    # Same raw V, per-instance scale: proves each block's own V_SF is read.
    assert [m.v for m in modules] == [pytest.approx(10.0), pytest.approx(1000.0)]


def test_mixed_scale_factor_scopes_are_rejected() -> None:
    # V stays scaled by the fixed block's A_SF while X uses an in-block V_SF.
    model = copy.deepcopy(MODEL_JSON)
    module = model["group"]["groups"][0]
    module["points"].append({"name": "X", "type": "uint16", "size": 1, "sf": "V_SF"})
    module["points"].append({"name": "V_SF", "type": "sunssf", "size": 1})
    with pytest.raises(SunSpecGenerationError, match="mixes in-block and"):
        generate_source([model])


def test_scale_factor_in_enclosing_block_is_rejected() -> None:
    model = copy.deepcopy(NESTED_MODEL_JSON)
    crv = model["group"]["groups"][0]
    crv["points"].append({"name": "C_SF", "type": "sunssf", "size": 1})
    crv["groups"][0]["points"][0]["sf"] = "C_SF"
    with pytest.raises(SunSpecGenerationError, match="enclosing repeating block"):
        generate_source([model])


def test_colliding_model_names_get_id_suffix() -> None:
    first = copy.deepcopy(MODEL_JSON)
    second = copy.deepcopy(MODEL_JSON)
    second["id"] = 64112
    source = generate_source([first, second])
    assert "class Test(SunSpecComponent):" in source
    assert "class Test64112(SunSpecComponent):" in source
    assert "module = repeating_group(uint16(11), Test64112Module, stride=3)" in source


def test_unknown_point_type_is_rejected() -> None:
    model = copy.deepcopy(MODEL_JSON)
    model["group"]["points"].append({"name": "X", "type": "mystery", "size": 1})
    with pytest.raises(SunSpecGenerationError, match="unsupported type"):
        generate_source([model])


# The models the generator cannot lay out statically today: 707-710 stack
# several device-sized blocks, so every block after the first has an
# unknowable address, and 63001 (SunSpec's test model) mixes in-block and
# fixed-block scale factors on one block. Newly supported or newly failing
# models both show up as a mismatch here - update the set deliberately.
# 707-710 do generate once --count supplies the device's counts, which is what
# test_generated_layout_matches_a_real_device covers.
KNOWN_UNSUPPORTED_MODELS = {707, 708, 709, 710, 63001}


def test_official_model_catalogue_generates_and_imports(official_models: Path) -> None:
    """Every published model either generates importable source or is
    rejected with a SunSpecGenerationError - never a crash or bad syntax."""
    generated: list[int] = []
    rejected: list[int] = []
    for path in sorted(official_models.glob("model_*.json")):
        model = json.loads(path.read_text())
        try:
            source = generate_source([model])
        except SunSpecGenerationError:
            rejected.append(model["id"])
            continue
        namespace: dict[str, Any] = {}
        # Importing exercises syntax, the generated imports and enum bodies.
        exec(compile(source, path.name, "exec"), namespace)  # noqa: S102
        generated.append(model["id"])

    assert len(generated) >= 100, (generated, rejected)
    assert set(rejected) == KNOWN_UNSUPPORTED_MODELS


def test_enum_named_after_label_at_module_level() -> None:
    model = copy.deepcopy(MODEL_JSON)
    st = next(p for p in model["group"]["points"] if p["name"] == "St")
    st["label"] = "Operating State"
    source = generate_source([model])
    assert "class OperatingState(IntEnum):" in source
    assert 'st = enum16(9, OperatingState)\n    """Operating State."""' in source


def test_identical_enums_are_shared_across_models() -> None:
    first = copy.deepcopy(MODEL_JSON)
    second = copy.deepcopy(MODEL_JSON)
    second["id"] = 64112
    source = generate_source([first, second])
    assert source.count("class St(IntEnum):") == 1
    assert "st = enum16(9, St)" in source  # both models reference the one enum


def test_conflicting_enum_names_get_owner_prefix() -> None:
    # The owner prefix keeps the label-based name: Test64112OperatingState,
    # not Test64112St (the point name is only used when there is no label).
    first = copy.deepcopy(MODEL_JSON)
    second = copy.deepcopy(MODEL_JSON)
    second["id"] = 64112
    for model in (first, second):
        st = next(p for p in model["group"]["points"] if p["name"] == "St")
        st["label"] = "Operating State"
    st = next(p for p in second["group"]["points"] if p["name"] == "St")
    st["symbols"] = [{"name": "IDLE", "value": 9}]
    source = generate_source([first, second])
    assert source.count("class OperatingState(IntEnum):") == 1
    assert "class Test64112OperatingState(IntEnum):" in source
    assert "st = enum16(9, Test64112OperatingState)" in source


# -- block writes ------------------------------------------------------------

# A 705-shaped model: a curve of writable points, each scaled from the fixed
# block, which is the layout the block-write helper exists for.
CURVE_MODEL_JSON: dict[str, Any] = {
    "id": 64333,
    "group": {
        "label": "Curve Model",
        "name": "curve",
        "type": "group",
        "points": [
            {"name": "ID", "type": "uint16", "size": 1},
            {"name": "L", "type": "uint16", "size": 1},
            {"name": "NPt", "type": "uint16", "size": 1},
            {"name": "V_SF", "type": "sunssf", "size": 1},
            {"name": "Var_SF", "type": "sunssf", "size": 1},
        ],
        "groups": [
            {
                "name": "Pt",
                "type": "group",
                "count": "NPt",
                "points": [
                    {
                        "name": "V",
                        "type": "uint16",
                        "size": 1,
                        "sf": "V_SF",
                        "access": "RW",
                    },
                    {
                        "name": "Var",
                        "type": "int16",
                        "size": 1,
                        "sf": "Var_SF",
                        "access": "RW",
                    },
                ],
            }
        ],
    },
}


def _nested(model: dict[str, Any], **outer: Any) -> dict[str, Any]:
    """Wrap the model's blocks in an enclosing block, as 705 nests Pt in Crv."""
    nested = copy.deepcopy(model)
    nested["group"]["groups"] = [
        {
            "name": "Crv",
            "type": "group",
            "points": [{"name": "ActPt", "type": "uint16", "size": 1}],
            "groups": nested["group"]["groups"],
            **outer,
        }
    ]
    return nested


def test_all_writable_block_gets_a_block_write_helper() -> None:
    source = generate_source([CURVE_MODEL_JSON], {64333: {"NPt": 3}})
    assert "pt = repeating_group(3, CurvePt, stride=2)" in source
    # A method on the block's owner, named after the block, over the helper.
    assert (
        "    async def write_pt(self, values: Sequence[Mapping[str, Any]]) -> None:"
        in source
    )
    assert '        await write_block(self, "pt", values)' in source
    # The docstring names the fields each mapping has to set.
    assert "        v, var. Instances past ``values`` are untouched." in source
    # The helper is generated into the module, not imported from the library.
    assert "async def write_block(" in source
    assert "from collections.abc import Mapping, Sequence" in source
    assert "from typing import Any" in source
    assert "write_group_block" not in source


def test_block_write_helper_is_emitted_once_per_module() -> None:
    # 707's shape: three sibling writable blocks share the one helper.
    model = copy.deepcopy(CURVE_MODEL_JSON)
    blocks = model["group"]["groups"]
    model["group"]["groups"] = [
        {**copy.deepcopy(blocks[0]), "name": name}
        for name in ("MustTrip", "MayTrip", "MomCess")
    ]
    source = generate_source([model], {64333: {"NPt": 2}})
    assert source.count("async def write_block(") == 1
    # One method per block, each named after its own block, so a class that
    # owns several - model 704 owns four - gets one method for each.
    assert [
        "write_must_trip",
        "write_may_trip",
        "write_mom_cess",
    ] == re.findall(r"async def (write_\w+)\(self", source)


def test_block_with_a_read_only_point_gets_no_helper() -> None:
    model = copy.deepcopy(CURVE_MODEL_JSON)
    model["group"]["groups"][0]["points"].append(
        {"name": "Sta", "type": "uint16", "size": 1}
    )
    source = generate_source([model], {64333: {"NPt": 3}})
    assert "repeating_group(3, CurvePt, stride=3)" in source
    assert "write_block" not in source


def test_poll_time_counted_block_still_gets_a_helper() -> None:
    # A block sized by a count point in its own scope is a repeating_group with
    # real instances once read, so it can be block written like a fixed one.
    source = generate_source([CURVE_MODEL_JSON])
    assert "pt = repeating_group(uint16(2), CurvePt, stride=2)" in source
    assert "async def write_block(" in source


def test_commented_out_block_gets_no_helper() -> None:
    # 705's shape: Pt sits inside Crv but is counted by the model's fixed block,
    # so without --count it is left commented - there is no group to write to.
    source = generate_source([_nested(CURVE_MODEL_JSON, count=2)])
    assert "# pt = repeating_group(<n>, CurveCrvPt, stride=2)" in source
    assert "write_block" not in source


async def test_generated_helper_writes_every_point_in_one_request() -> None:
    """A generated curve programs all its points in a single write."""
    source = generate_source([_nested(CURVE_MODEL_JSON, count=2)], {64333: {"NPt": 4}})
    namespace: dict[str, Any] = {}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102

    base, length = 40002, 21  # 3 fixed data points + 2 curves * 9
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update(dict.fromkeys(range(base, base + length + 2), 0))
    unit.holding[base] = 64333
    unit.holding[base + 1] = length
    unit.holding[base + 3] = (-2) & 0xFFFF  # V_SF
    unit.holding[base + 4] = 0  # Var_SF
    component = namespace["Curve"](
        unit, SunSpecModel(model_id=64333, address=base, length=length)
    )
    await component.async_update()

    writes: list[Any] = []
    unit.on_write(writes.append)
    reads = unit.read_events
    reads.clear()
    await component.crv[1].write_pt(
        [
            {"v": 92.0, "var": 30},
            {"v": 98.0, "var": 0},
            {"v": 102.0, "var": 0},
            {"v": 108.0, "var": -30},
        ]
    )

    # Eight registers in one request, and one read per distinct scale register.
    assert len(writes) == 1
    assert writes[0].function_code == 16
    assert writes[0].values == [9200, 30, 9800, 0, 10200, 0, 10800, (-30) & 0xFFFF]
    assert len(reads) == 2

    await component.async_update()
    assert [(point.v, point.var) for point in component.crv[1].pt] == [
        (92.0, 30),
        (98.0, 0),
        (102.0, 0),
        (108.0, -30),
    ]
    assert all(point.v == 0.0 for point in component.crv[0].pt)


async def test_generated_helper_leaves_later_instances_untouched() -> None:
    source = generate_source([CURVE_MODEL_JSON], {64333: {"NPt": 3}})
    namespace: dict[str, Any] = {}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102

    base, length = 40002, 9
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update(dict.fromkeys(range(base, base + length + 2), 0))
    unit.holding[base] = 64333
    unit.holding[base + 1] = length
    unit.holding[base + 7] = 7  # the third point
    unit.holding[base + 8] = 7
    component = namespace["Curve"](
        unit, SunSpecModel(model_id=64333, address=base, length=length)
    )
    await component.async_update()

    await component.write_pt([{"v": 1, "var": 2}])
    assert await unit.read_holding_registers(base + 7, 2) == [7, 7]


async def test_generated_helper_rejects_more_values_than_instances() -> None:
    source = generate_source([CURVE_MODEL_JSON], {64333: {"NPt": 2}})
    namespace: dict[str, Any] = {}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102

    base, length = 40002, 7
    unit = MockModbusConnection().for_unit(1)
    unit.holding.update(dict.fromkeys(range(base, base + length + 2), 0))
    unit.holding[base] = 64333
    unit.holding[base + 1] = length
    component = namespace["Curve"](
        unit, SunSpecModel(model_id=64333, address=base, length=length)
    )
    await component.async_update()

    with pytest.raises(IndexError, match="has 2 instance"):
        await component.write_pt([{"v": 1, "var": 2}] * 3)
