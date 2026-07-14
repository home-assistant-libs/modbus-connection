"""Generate typed SunSpec components from the official model definitions.

SunSpec publishes every standard information model as JSON in
`sunspec/models <https://github.com/sunspec/models>`_ (``json/model_N.json``).
This module turns those definitions into Python source: one
:class:`~modbus_connection.model.sunspec.SunSpecComponent` subclass per model,
with a field per point (addresses, scale-factor registers, units, writability
and the per-type unimplemented sentinels all wired up), ``IntEnum`` /
``IntFlag`` classes for enumerated and bitfield points, and a
:func:`~modbus_connection.model.repeating_group` for a repeating block.

Run it as a script with model IDs (fetched from the official repository) or
paths to local ``model_N.json`` files::

    python -m modbus_connection.model.sunspec.generate 1 103 160
    python -m modbus_connection.model.sunspec.generate -o models.py model_103.json

or call :func:`generate_source` with already-parsed model JSON.

The generator is a helper to get an integration started: the emitted classes
are base classes to commit as ordinary source, not a build artifact. Devices
routinely deviate from the published models, so expect to trim and adjust the
generated classes to the manufacturer's actual implementation. Pair them with
:func:`~modbus_connection.model.sunspec.scan` to place them at the discovered
model addresses.
"""

from __future__ import annotations

import argparse
import json
import keyword
import re
import sys
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..component import Component
from . import SunSpecComponent

_MODEL_URL = (
    "https://raw.githubusercontent.com/sunspec/models/master/json/model_{model_id}.json"
)

# Point types emitted as a same-named numeric factory call.
_NUMERIC_TYPES = frozenset({"int16", "uint16", "int32", "uint32", "int64", "uint64"})
_ACC_TYPES = frozenset({"acc16", "acc32", "acc64"})
_ENUM_TYPES = frozenset({"enum16", "enum32"})
_BITFIELD_TYPES = frozenset({"bitfield16", "bitfield32", "bitfield64"})
_PLAIN_TYPES = frozenset(
    {"sunssf", "float32", "float64", "ipaddr", "ipv6addr", "eui48"}
)

# Attribute names the generated classes must not shadow: everything the
# component base classes already define, plus the factory names the generated
# module imports at top level (a field named after a factory would break later
# fields in the same class body).
_RESERVED_ATTRS = frozenset(dir(Component)) | frozenset(dir(SunSpecComponent))


class SunSpecGenerationError(Exception):
    """A model definition cannot be expressed as a static component layout."""


@dataclass
class _Point:
    """One point of a model group, placed at its model-relative address."""

    name: str
    type: str
    size: int
    address: int
    sf: str | int | None
    units: str | None
    writable: bool
    label: str | None
    symbols: list[tuple[str, int]]


def _snake(name: str) -> str:
    """Convert a SunSpec point name (``AphA``, ``DCA_SF``) to snake_case."""
    s = re.sub(r"[^0-9A-Za-z]+", "_", name)
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", s)
    s = re.sub(r"__+", "_", s).strip("_").lower()
    if not s or s[0].isdigit():
        s = f"p_{s}"
    if keyword.iskeyword(s):
        s += "_"
    return s


def _camel(name: str) -> str:
    """Convert a group or point name to a CamelCase class name."""
    parts = [p for p in re.split(r"[^0-9A-Za-z]+", name) if p]
    return "".join(p[:1].upper() + p[1:] for p in parts) or "Group"


def _member(name: str) -> str:
    """Sanitize a symbol name into a valid enum member name."""
    s = re.sub(r"[^0-9A-Za-z]+", "_", name).strip("_")
    if not s or s[0].isdigit():
        s = f"V_{s}"
    if keyword.iskeyword(s):
        s += "_"
    return s


def _parse_points(raw_points: list[Any], start: int) -> list[_Point]:
    """Assign consecutive model-relative addresses to a group's points."""
    points = []
    address = start
    for raw in raw_points:
        size = int(raw["size"])
        points.append(
            _Point(
                name=raw["name"],
                type=raw["type"],
                size=size,
                address=address,
                sf=raw.get("sf"),
                units=raw.get("units"),
                writable=raw.get("access") == "RW",
                label=raw.get("label"),
                symbols=[(s["name"], int(s["value"])) for s in raw.get("symbols", [])],
            )
        )
        address += size
    return points


class _ClassWriter:
    """Accumulates the body of one generated component class."""

    def __init__(self, name: str, base: str, docstring: str) -> None:
        self.name = name
        self.base = base
        self.docstring = docstring
        self.enum_lines: list[str] = []
        self.field_lines: list[str] = []
        self._attrs: set[str] = set()

    def attr_name(self, point_name: str) -> str:
        """A unique, non-shadowing snake_case attribute for a point."""
        attr = _snake(point_name)
        while attr in _RESERVED_ATTRS or attr in self._attrs:
            attr += "_"
        self._attrs.add(attr)
        return attr

    def render(self) -> str:
        lines = [f"class {self.name}({self.base}):", f'    """{self.docstring}"""']
        for block in (self.enum_lines, self.field_lines):
            while block and not block[-1]:
                block.pop()
            if block:
                lines.append("")
                lines.extend(block)
        if not self.enum_lines and not self.field_lines:
            lines.append("")
            lines.append("    # This model defines no points beyond the header.")
        return "\n".join(lines)


class _ModuleWriter:
    """Accumulates the generated module: imports and rendered classes."""

    def __init__(self) -> None:
        self.enum_imports: set[str] = set()
        self.model_imports: set[str] = set()
        self.sunspec_imports: set[str] = set()
        self.classes: list[str] = []
        self.sources: list[str] = []

    def render(self) -> str:
        header = [
            '"""SunSpec components generated from the official model definitions.',
            "",
            f"Source: https://github.com/sunspec/models ({', '.join(self.sources)})",
            "Generated by python -m modbus_connection.model.sunspec.generate as",
            "a starting point; devices deviate from the published models, so",
            "adjust these classes to the manufacturer's actual implementation.",
            '"""',
            "",
            "from __future__ import annotations",
            "",
        ]
        if self.enum_imports:
            header.append(f"from enum import {', '.join(sorted(self.enum_imports))}")
            header.append("")
        if self.model_imports:
            header.append(
                "from modbus_connection.model import "
                + ", ".join(sorted(self.model_imports))
            )
        names = sorted(self.sunspec_imports)
        header.append("from modbus_connection.model.sunspec import (")
        header.extend(f"    {name}," for name in names)
        header.append(")")
        return "\n".join(header) + "\n\n\n" + "\n\n\n".join(self.classes) + "\n"


def _emit_point(
    point: _Point,
    writer: _ClassWriter,
    module: _ModuleWriter,
    scale_addresses: Mapping[str, int],
    model_id: int,
) -> None:
    """Append one point's field (and enum class, if any) to its class body."""
    attr = writer.attr_name(point.name)
    args = [str(point.address)]
    kwargs: list[str] = []
    if point.type in _NUMERIC_TYPES or point.type in _ACC_TYPES:
        factory = point.type
        if isinstance(point.sf, str):
            if point.sf not in scale_addresses:
                raise SunSpecGenerationError(
                    f"model {model_id}: point {point.name} references scale"
                    f" factor {point.sf}, which is not in the fixed block or"
                    " its own group"
                )
            kwargs.append(f"scale_register={scale_addresses[point.sf]}")
        elif isinstance(point.sf, int):
            kwargs.append(f"scale={10.0**point.sf!r}")
        if point.writable and point.type in _NUMERIC_TYPES:
            kwargs.append("writable=True")
        if point.units is not None:
            kwargs.append(f"unit={point.units!r}")
    elif point.type in _ENUM_TYPES or point.type in _BITFIELD_TYPES:
        factory = point.type
        if point.symbols:
            is_flag = point.type in _BITFIELD_TYPES
            enum_base = "IntFlag" if is_flag else "IntEnum"
            module.enum_imports.add(enum_base)
            enum_name = _camel(point.name)
            label = f"  # {point.label}" if point.label else ""
            writer.enum_lines.append(f"    class {enum_name}({enum_base}):{label}")
            for symbol, value in point.symbols:
                literal = f"1 << {value}" if is_flag else str(value)
                writer.enum_lines.append(f"        {_member(symbol)} = {literal}")
            writer.enum_lines.append("")
            args.append(enum_name)
        if point.writable:
            kwargs.append("writable=True")
    elif point.type == "string":
        factory = "string"
        args.append(str(point.size))
        if point.writable:
            kwargs.append("writable=True")
    elif point.type == "count":
        # The repeat-count point; also read from repeating_group, see below.
        factory = "uint16"
    elif point.type in _PLAIN_TYPES:
        factory = point.type
        if point.writable and point.type in ("float32", "float64"):
            kwargs.append("writable=True")
        if point.units is not None and point.type in ("float32", "float64"):
            kwargs.append(f"unit={point.units!r}")
    else:
        raise SunSpecGenerationError(
            f"model {model_id}: point {point.name} has unsupported type {point.type!r}"
        )
    module.sunspec_imports.add(factory)
    call = f"{factory}({', '.join(args + kwargs)})"
    label = f"  # {point.label}" if point.label else ""
    writer.field_lines.append(f"    {attr} = {call}{label}")


def _count_expression(
    raw_count: Any,
    fixed_points: list[_Point],
    module: _ModuleWriter,
    model_id: int,
    group_name: str,
) -> str | None:
    """The ``repeating_group`` count: a fixed int or a count-point field.

    A string count names the point holding the repeat count; ``0`` means the
    block repeats to fill the model length, which the official models pair
    with a ``count``-type point in the fixed block — used when it is
    unambiguous. Returns ``None`` when no count can be determined.
    """
    if isinstance(raw_count, str):
        for point in fixed_points:
            if point.name == raw_count:
                module.sunspec_imports.add("uint16")
                return f"uint16({point.address})"
        raise SunSpecGenerationError(
            f"model {model_id}: group {group_name} count references point"
            f" {raw_count!r}, which is not in the fixed block"
        )
    count = int(raw_count)
    if count > 0:
        return str(count)
    count_points = [p for p in fixed_points if p.type == "count"]
    if len(count_points) == 1:
        module.sunspec_imports.add("uint16")
        return f"uint16({count_points[0].address})"
    return None


def _generate_model(model: Mapping[str, Any], module: _ModuleWriter) -> None:
    """Append one model's classes (repeating blocks first) to the module."""
    model_id = int(model["id"])
    group = model["group"]
    module.sources.append(f"json/model_{model_id}.json")

    fixed_points = _parse_points(group.get("points", []), 0)
    # ID and L are the model header; SunSpecComponent already declares them
    # (model_id / model_length at 0 and 1), so they are parsed for the address
    # walk but not emitted.
    data_points = [p for p in fixed_points if p.name not in ("ID", "L")]
    scale_addresses = {p.name: p.address for p in fixed_points if p.type == "sunssf"}

    label = group.get("label") or group.get("name") or ""
    class_name = f"Model{model_id}"
    writer = _ClassWriter(
        class_name, "SunSpecComponent", f"SunSpec model {model_id}: {label}."
    )
    module.sunspec_imports.add("SunSpecComponent")

    offset = fixed_points[-1].address + fixed_points[-1].size if fixed_points else 0
    group_lines: list[str] = []
    subgroups = group.get("groups", [])
    for position, subgroup in enumerate(subgroups):
        if subgroup.get("groups"):
            raise SunSpecGenerationError(
                f"model {model_id}: group {subgroup.get('name')!r} contains"
                " nested groups, which cannot be laid out statically"
            )
        points = _parse_points(subgroup.get("points", []), offset)
        stride = sum(p.size for p in points)
        group_scales = dict(scale_addresses)
        for point in points:
            if point.type == "sunssf":
                # A scale factor inside the block would shift per instance,
                # which scale_register addresses never do (they follow the
                # shared fixed block) - no static layout can express it.
                raise SunSpecGenerationError(
                    f"model {model_id}: group {subgroup['name']} defines scale"
                    f" factor {point.name} inside the repeating block"
                )
        group_class = f"{class_name}{_camel(subgroup['name'])}"
        group_writer = _ClassWriter(
            group_class,
            "Component",
            f"One {subgroup['name']!r} block of SunSpec model {model_id}.",
        )
        module.model_imports.add("Component")
        for point in points:
            if point.type == "pad":
                continue
            _emit_point(point, group_writer, module, group_scales, model_id)
        module.classes.append(group_writer.render())

        attr = writer.attr_name(subgroup["name"])
        count_expr = _count_expression(
            subgroup.get("count", 1),
            fixed_points,
            module,
            model_id,
            subgroup["name"],
        )
        if count_expr is None:
            group_lines.append(
                f"    # {subgroup['name']!r} repeats to fill the model length"
                " and defines no count point; size it from the scanned"
                " model.length:"
            )
            group_lines.append(
                f"    # {attr} = repeating_group(N, {group_class}, stride={stride})"
            )
        else:
            module.model_imports.add("repeating_group")
            group_lines.append(
                f"    {attr} = repeating_group({count_expr}, {group_class},"
                f" stride={stride})"
            )
        if isinstance(subgroup.get("count", 1), int) and subgroup.get("count", 1) > 0:
            offset += int(subgroup["count"]) * stride
        elif position != len(subgroups) - 1:
            raise SunSpecGenerationError(
                f"model {model_id}: group {subgroup['name']!r} has a dynamic"
                " count but is not the last group, so later addresses are"
                " unknown"
            )

    for point in data_points:
        if point.type == "pad":
            continue
        _emit_point(point, writer, module, scale_addresses, model_id)
    writer.field_lines.extend(group_lines)
    module.classes.append(writer.render())


def generate_source(models: Iterable[Mapping[str, Any]]) -> str:
    """Render parsed SunSpec model JSON into a Python module's source."""
    module = _ModuleWriter()
    for model in models:
        _generate_model(model, module)
    if not module.classes:
        raise SunSpecGenerationError("no models given")
    return module.render()


def _load(spec: str) -> Any:
    """Load one model: a numeric ID (fetched from GitHub) or a local path."""
    if spec.isdigit():
        url = _MODEL_URL.format(model_id=int(spec))
        with urllib.request.urlopen(url) as response:
            return json.load(response)
    with open(spec, encoding="utf-8") as file:
        return json.load(file)


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        prog="python -m modbus_connection.model.sunspec.generate",
        description=(
            "Generate typed SunSpec components from the official model"
            " definitions (https://github.com/sunspec/models)."
        ),
    )
    parser.add_argument(
        "models",
        nargs="+",
        help="SunSpec model IDs (fetched from the official repository)"
        " or paths to local model_N.json files",
    )
    parser.add_argument(
        "-o",
        "--out",
        help="write the generated module here instead of stdout",
    )
    options = parser.parse_args(argv)
    try:
        source = generate_source(_load(spec) for spec in options.models)
    except (OSError, SunSpecGenerationError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    if options.out:
        with open(options.out, "w", encoding="utf-8") as file:
            file.write(source)
    else:
        sys.stdout.write(source)
    return 0


if __name__ == "__main__":
    sys.exit(main())
