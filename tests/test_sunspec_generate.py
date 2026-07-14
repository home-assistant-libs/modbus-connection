"""Tests for the SunSpec model generator (modbus_connection.model.sunspec.generate)."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from modbus_connection.mock import MockModbusConnection
from modbus_connection.model.sunspec import SunSpecModel
from modbus_connection.model.sunspec.generate import (
    SunSpecGenerationError,
    _member,
    _snake,
    generate_source,
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
    assert "a_sf = sunssf(3)" in source
    assert "offs = int16(4, scale=0.1)" in source
    assert "da = uint16(5, writable=True)" in source
    assert "mn = string(6, 2)  # Manufacturer" in source
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
    assert "class Model64111Module(Component):" in source
    assert "v = uint16(14, scale_register=3, unit='V')" in source
    assert "lbl = string(15, 2)" in source
    assert "module = repeating_group(uint16(11), Model64111Module, stride=3)" in source


async def test_generated_module_decodes() -> None:
    source = generate_source([MODEL_JSON])
    namespace: dict[str, Any] = {}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102
    model_cls = namespace["Model64111"]

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

    assert component.a == pytest.approx(12.34)
    assert component.a_sf == -2
    assert component.offs == pytest.approx(2.5)
    assert component.da == 7
    assert component.mn == "AB"
    assert component.st == model_cls.St.ON
    assert component.evt == model_cls.Evt.FAULT | model_cls.Evt.WARN
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
    assert "# module = repeating_group(N, Model64111Module, stride=3)" in source


def test_string_count_reference() -> None:
    model = copy.deepcopy(MODEL_JSON)
    model["group"]["groups"][0]["count"] = "N"
    source = generate_source([model])
    assert "module = repeating_group(uint16(11), Model64111Module, stride=3)" in source


def test_fixed_count_folds_into_layout() -> None:
    model = copy.deepcopy(MODEL_JSON)
    model["group"]["groups"][0]["count"] = 3
    source = generate_source([model])
    assert "module = repeating_group(3, Model64111Module, stride=3)" in source


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
    assert "class Model64111ModuleChan(Component):" in source
    assert "val = uint16(17)" in source
    assert "chan = repeating_group(2, Model64111ModuleChan, stride=1)" in source
    assert "module = repeating_group(uint16(11), Model64111Module, stride=5)" in source


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
    component = namespace["Model64111"](
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
    assert "class Model64222CrvPt(Component):" in source
    assert "v = uint16(5)" in source
    assert "class Model64222Crv(Component):" in source
    assert "act_pt = uint16(4)" in source
    # The inner count lives in the top block, so it cannot be wired (it would
    # shift with the curve instance); the stride is known.
    assert "# pt = repeating_group(N, Model64222CrvPt, stride=1)" in source
    # The outer count wires, but the stride depends on the device's NPt.
    assert "# crv = repeating_group(uint16(3), Model64222Crv, stride=<...>)" in source


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


def test_scale_factor_inside_repeating_block_is_rejected() -> None:
    model = copy.deepcopy(MODEL_JSON)
    model["group"]["groups"][0]["points"].append(
        {"name": "V_SF", "type": "sunssf", "size": 1}
    )
    with pytest.raises(SunSpecGenerationError, match="inside the repeating block"):
        generate_source([model])


def test_unknown_point_type_is_rejected() -> None:
    model = copy.deepcopy(MODEL_JSON)
    model["group"]["points"].append({"name": "X", "type": "mystery", "size": 1})
    with pytest.raises(SunSpecGenerationError, match="unsupported type"):
        generate_source([model])
