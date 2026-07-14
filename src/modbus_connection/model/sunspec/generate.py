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


@dataclass
class _Group:
    """One (possibly nested) block of a model, placed at its instance-0 offset."""

    name: str
    raw_count: Any  # int, or the name of the point holding the repeat count
    points: list[_Point]
    children: list[_Group]
    size: int | None  # registers per instance; None when only the device knows


def _fixed_count(raw_count: Any) -> int | None:
    """The statically-known repeat count, or None for a device-sized block."""
    if isinstance(raw_count, int) and raw_count > 0:
        return raw_count
    return None


def _parse_group(
    raw: Mapping[str, Any], start: int, model_id: int, *, top: bool = False
) -> _Group:
    """Recursively place a block's points and nested blocks from ``start``.

    Sizes propagate bottom-up: a block sized by a device-read count has no
    static size, and neither does any block containing one, so nothing may
    follow such a block at any level.
    """
    points = _parse_points(raw.get("points", []), start)
    if not top:
        for point in points:
            if point.type == "sunssf":
                # A scale factor inside the block would shift per instance,
                # which scale_register addresses never do (they follow the
                # shared fixed block) - no static layout can express it.
                raise SunSpecGenerationError(
                    f"model {model_id}: group {raw['name']} defines scale"
                    f" factor {point.name} inside the repeating block"
                )
    offset = start + sum(p.size for p in points)
    children: list[_Group] = []
    size: int | None = offset - start
    for sub in raw.get("groups", []):
        if size is None:
            raise SunSpecGenerationError(
                f"model {model_id}: group {children[-1].name!r} has a"
                " device-dependent size but is not the last block, so later"
                " addresses are unknown"
            )
        child = _parse_group(sub, offset, model_id)
        children.append(child)
        count = _fixed_count(child.raw_count)
        if count is not None and child.size is not None:
            offset += count * child.size
            size = offset - start
        else:
            size = None
    return _Group(raw.get("name", ""), raw.get("count", 1), points, children, size)


def _count_expression(
    raw_count: Any,
    scopes: list[_Group],
    module: _ModuleWriter,
    model_id: int,
    group_name: str,
) -> str | None:
    """The ``repeating_group`` count: a fixed int or a count-point field.

    A string count names the point holding the repeat count; it wires
    statically only when that point sits in the enclosing block itself
    (``scopes[-1]``) — a count register anywhere else would shift with the
    wrong instance. ``0`` means the block repeats to fill the model length,
    which the official models pair with a ``count``-type point in the
    enclosing block — used when it is unambiguous. Returns ``None`` when the
    count must be supplied at runtime.
    """
    if isinstance(raw_count, str):
        for point in scopes[-1].points:
            if point.name == raw_count:
                module.sunspec_imports.add("uint16")
                return f"uint16({point.address})"
        if any(point.name == raw_count for scope in scopes for point in scope.points):
            return None
        raise SunSpecGenerationError(
            f"model {model_id}: group {group_name} count references point"
            f" {raw_count!r}, which is not defined in the model"
        )
    count = int(raw_count)
    if count > 0:
        return str(count)
    count_points = [p for p in scopes[-1].points if p.type == "count"]
    if len(count_points) == 1:
        module.sunspec_imports.add("uint16")
        return f"uint16({count_points[0].address})"
    return None


def _wire_child(
    child: _Group,
    child_class: str,
    scopes: list[_Group],
    writer: _ClassWriter,
    module: _ModuleWriter,
    model_id: int,
) -> list[str]:
    """Wire one nested block on its parent: a ``repeating_group`` or a hint.

    The wiring is static only when both halves are: the count (see
    :func:`_count_expression`) and the stride — a block whose size depends on
    a device-read count cannot be placed at class-definition time, so it gets
    a commented-out line to fill in at runtime instead.
    """
    attr = writer.attr_name(child.name)
    count_expr = _count_expression(
        child.raw_count, scopes, module, model_id, child.name
    )
    if count_expr is not None and child.size:
        module.model_imports.add("repeating_group")
        return [
            f"    {attr} = repeating_group({count_expr}, {child_class},"
            f" stride={child.size})"
        ]
    lines = []
    if count_expr is None and isinstance(child.raw_count, str):
        lines.append(
            f"    # {child.name!r} is sized by {child.raw_count!r}, which"
            " lives outside this"
        )
        lines.append(
            "    # block and would shift with the wrong instance; wire it"
            " with the value"
        )
        lines.append("    # read from the device:")
    elif count_expr is None:
        lines.append(
            f"    # {child.name!r} repeats to fill the model length and"
            " defines no count"
        )
        lines.append("    # point; size it from the scanned model.length:")
    else:
        lines.append(
            f"    # {child.name!r} has a device-dependent size, so its stride"
            " (and anything"
        )
        lines.append(
            "    # behind it) is only known at runtime; place it with the sizes read"
        )
        lines.append("    # from the device:")
    stride = child.size if child.size else "<...>"
    lines.append(
        f"    # {attr} = repeating_group({count_expr or 'N'}, {child_class},"
        f" stride={stride})"
    )
    return lines


def _emit_group_class(
    group: _Group,
    prefix: str,
    module: _ModuleWriter,
    scale_addresses: Mapping[str, int],
    model_id: int,
    scopes: list[_Group],
) -> str:
    """Emit one block's ``Component`` class (children first); return its name."""
    class_name = f"{prefix}{_camel(group.name)}"
    writer = _ClassWriter(
        class_name,
        "Component",
        f"One {group.name!r} block of SunSpec model {model_id}.",
    )
    module.model_imports.add("Component")
    wiring: list[str] = []
    for child in group.children:
        child_class = _emit_group_class(
            child, class_name, module, scale_addresses, model_id, [*scopes, group]
        )
        wiring.extend(
            _wire_child(child, child_class, [*scopes, group], writer, module, model_id)
        )
    for point in group.points:
        if point.type != "pad":
            _emit_point(point, writer, module, scale_addresses, model_id)
    writer.field_lines.extend(wiring)
    module.classes.append(writer.render())
    return class_name


def _generate_model(model: Mapping[str, Any], module: _ModuleWriter) -> None:
    """Append one model's classes (innermost blocks first) to the module."""
    model_id = int(model["id"])
    module.sources.append(f"json/model_{model_id}.json")

    top = _parse_group(model["group"], 0, model_id, top=True)
    # ID and L are the model header; SunSpecComponent already declares them
    # (model_id / model_length at 0 and 1), so they are parsed for the address
    # walk but not emitted.
    data_points = [p for p in top.points if p.name not in ("ID", "L")]
    scale_addresses = {p.name: p.address for p in top.points if p.type == "sunssf"}

    label = model["group"].get("label") or top.name
    class_name = f"Model{model_id}"
    writer = _ClassWriter(
        class_name, "SunSpecComponent", f"SunSpec model {model_id}: {label}."
    )
    module.sunspec_imports.add("SunSpecComponent")

    wiring: list[str] = []
    for child in top.children:
        child_class = _emit_group_class(
            child, class_name, module, scale_addresses, model_id, [top]
        )
        wiring.extend(_wire_child(child, child_class, [top], writer, module, model_id))
    for point in data_points:
        if point.type != "pad":
            _emit_point(point, writer, module, scale_addresses, model_id)
    writer.field_lines.extend(wiring)
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
