"""Building blocks for a device *query helper* — a standalone CLI that connects
to a real device, reads it once, and prints every value.

The package itself never imports this module, so nothing here is pulled into a
normal application. A query script imports the pieces it needs instead of
re-implementing them every time:

- :func:`add_connection_args` / :func:`connect_from_args` — turn command-line
  arguments into a live connection (over the tmodbus backend).
- :class:`CountingUnit` — wrap a ``ModbusUnit`` to count the reads it performs,
  so you can see how well the pooled read plan collapses your fields.
- :func:`print_component` / :func:`field_rows` — dump a modelled component's
  fields to the terminal by reflection, no hand-listing.

Only :func:`connect_from_args` needs a backend installed (the ``[tmodbus]``
extra); the counter and the printer are backend-neutral, so ``--help`` and the
argument parsing work without one.

A minimal query script::

    import argparse
    import asyncio
    from typing import cast

    from modbus_connection import ModbusError, ModbusUnit
    from modbus_connection.cli_helper import (
        CountingUnit,
        add_connection_args,
        connect_from_args,
        print_component,
    )


    async def main() -> int:
        parser = argparse.ArgumentParser(description="Query a device.")
        add_connection_args(parser)
        parser.add_argument("--unit", type=int, default=1, help="Modbus unit id")
        args = parser.parse_args()

        try:
            conn = await connect_from_args(args)
        except ModbusError as err:
            print(f"Could not connect: {err}")
            return 1
        counting = CountingUnit(conn.for_unit(args.unit))
        try:
            device = MyDevice(cast(ModbusUnit, counting))  # your modelled component
            await device.async_update()
        finally:
            await conn.close()

        print_component(device)
        print(f"\n{counting.reads} Modbus reads")
        return 0


    raise SystemExit(asyncio.run(main()))

The unit id is not part of connecting — it varies per device and per tool — so
add whatever the CLI needs (like ``--unit`` above) alongside the connection
arguments.
"""

from __future__ import annotations

import argparse
import inspect
import sys
from enum import IntEnum
from typing import TYPE_CHECKING

from ._protocol import ModbusConnection, ModbusUnit
from .model import CoilField, Component, DiscreteInputField, RegisterField

if TYPE_CHECKING:
    from typing import TextIO

__all__ = [
    "CountingUnit",
    "add_connection_args",
    "connect_from_args",
    "field_rows",
    "print_component",
]


# -- connection from arguments -----------------------------------------------


def add_connection_args(parser: argparse.ArgumentParser) -> argparse._ArgumentGroup:
    """Add the connection-specifying arguments to *parser*.

    The arguments land in their own "Modbus connection" group, so they read as a
    block in ``--help`` and stay clear of the CLI's own options. Returns the
    group in case the caller wants to tweak it.

    ``connect_from_args`` consumes exactly what this adds; keep the two together.
    """
    group = parser.add_argument_group("Modbus connection")
    group.add_argument(
        "target",
        help="host or IP for tcp/tls, or the serial device path for serial",
    )
    group.add_argument(
        "--transport",
        choices=("tcp", "tls", "serial"),
        default="tcp",
        help="wire transport (default: tcp)",
    )
    group.add_argument(
        "--port",
        type=int,
        default=None,
        help="TCP/TLS port (default: 502 for tcp, 802 for tls)",
    )
    group.add_argument(
        "--framer",
        choices=("socket", "rtu", "ascii"),
        default=None,
        help=(
            "wire framing for tcp (socket/rtu) or serial (rtu/ascii); "
            "backend default if unset"
        ),
    )
    group.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="per-request timeout in seconds (default: 3)",
    )
    group.add_argument(
        "--message-spacing",
        type=float,
        default=0.0,
        help="minimum gap in seconds left after each request (default: 0)",
    )

    serial = parser.add_argument_group("Modbus serial")
    serial.add_argument("--baudrate", type=int, default=9600)
    serial.add_argument("--bytesize", type=int, choices=(7, 8), default=8)
    serial.add_argument("--parity", choices=("N", "E", "O"), default="N")
    serial.add_argument("--stopbits", type=int, choices=(1, 2), default=1)

    tls = parser.add_argument_group("Modbus/TLS")
    tls.add_argument(
        "--tls-no-verify",
        action="store_true",
        help="do not verify the server certificate (self-signed devices)",
    )
    tls.add_argument(
        "--tls-ca",
        default=None,
        help="verify against this CA bundle file or directory instead",
    )
    tls.add_argument(
        "--tls-no-check-hostname",
        action="store_true",
        help="verify the certificate but not the hostname",
    )
    tls.add_argument("--tls-client-cert", default=None, help="client cert for mTLS")
    tls.add_argument("--tls-client-key", default=None, help="client key for mTLS")
    tls.add_argument("--tls-client-key-password", default=None)
    return group


async def connect_from_args(args: argparse.Namespace) -> ModbusConnection:
    """Open the connection described by *args* (as parsed by ``add_connection_args``).

    Dispatches to ``connect_tcp`` / ``connect_tls`` / ``connect_serial`` on the
    tmodbus backend, imported here so importing this module needs no backend.
    Raises ``ModbusConnectionError`` if the connection cannot be established, or
    ``ValueError`` for a bad framer/transport combination.
    """
    # Imported lazily so the module (and --help) loads without the backend.
    from .tmodbus import connect_serial, connect_tcp, connect_tls

    common = {"timeout": args.timeout, "message_spacing": args.message_spacing}

    if args.transport == "serial":
        return await connect_serial(
            args.target,
            baudrate=args.baudrate,
            bytesize=args.bytesize,
            parity=args.parity,
            stopbits=args.stopbits,
            **({"framer": args.framer} if args.framer else {}),
            **common,
        )

    if args.transport == "tls":
        verify: bool | str
        if args.tls_no_verify:
            verify = False
        elif args.tls_ca:
            verify = args.tls_ca
        else:
            verify = True
        return await connect_tls(
            args.target,
            verify=verify,
            check_hostname=not args.tls_no_check_hostname,
            client_cert=args.tls_client_cert,
            client_key=args.tls_client_key,
            client_key_password=args.tls_client_key_password,
            **({"port": args.port} if args.port is not None else {}),
            **common,
        )

    return await connect_tcp(
        args.target,
        **({"port": args.port} if args.port is not None else {}),
        **({"framer": args.framer} if args.framer else {}),
        **common,
    )


# -- read counting -----------------------------------------------------------


class CountingUnit:
    """Wrap a ``ModbusUnit`` to count the reads it performs.

    Pass ``connection.for_unit(id)`` through here before handing it to a
    component; ``reads`` then tallies every read the update issued — a quick
    sanity check that your ``ranges`` and ``max_gap`` are collapsing fields into
    as few Modbus round-trips as the plan allows. The four read methods are
    counted; every other ``ModbusUnit`` method is delegated untouched, so it
    satisfies the protocol structurally (``cast`` it to ``ModbusUnit`` for a type
    checker).
    """

    def __init__(self, unit: ModbusUnit) -> None:
        self._unit = unit
        self.reads = 0

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        self.reads += 1
        return await self._unit.read_holding_registers(address, count)

    async def read_input_registers(self, address: int, count: int) -> list[int]:
        self.reads += 1
        return await self._unit.read_input_registers(address, count)

    async def read_coils(self, address: int, count: int) -> list[bool]:
        self.reads += 1
        return await self._unit.read_coils(address, count)

    async def read_discrete_inputs(self, address: int, count: int) -> list[bool]:
        self.reads += 1
        return await self._unit.read_discrete_inputs(address, count)

    def __getattr__(self, name: str) -> object:
        return getattr(self._unit, name)


# -- field reflection --------------------------------------------------------


def _format_value(value: object) -> str:
    """Render a decoded field value for display."""
    if value is None:
        return "—"
    if isinstance(value, IntEnum):
        return value.name.lower()
    return str(value)


def field_rows(component: Component) -> list[tuple[str, str]]:
    """Return ``(name, value)`` rows for every field on *component*, by reflection.

    Walks the component's public attributes and keeps the modelled ones —
    register/coil/discrete fields and computed ``@property`` values — skipping
    methods and internals. A field's ``unit`` label (see
    :class:`~modbus_connection.model.RegisterField`) is appended to its value.
    Read the component (``await component.async_update()``) first; unread fields
    render as ``—``.
    """
    cls = type(component)
    rows: list[tuple[str, str]] = []
    for name in dir(component):
        if name.startswith("_"):
            continue
        descriptor = inspect.getattr_static(cls, name, None)
        if not isinstance(
            descriptor, (RegisterField, CoilField, DiscreteInputField, property)
        ):
            continue
        value = _format_value(getattr(component, name))
        unit = descriptor.unit if isinstance(descriptor, RegisterField) else None
        rows.append((name, f"{value} {unit}" if unit else value))
    return rows


def print_component(
    component: Component,
    *,
    title: str | None = None,
    file: TextIO | None = None,
) -> None:
    """Print every field on *component* under a heading, values aligned.

    *title* defaults to the component's class name. Reflection-based, so a new
    field shows up with no change here. Read the component first (see
    :func:`field_rows`).
    """
    rows = field_rows(component)
    out = file if file is not None else sys.stdout
    heading = title if title is not None else type(component).__name__
    print(heading, file=out)
    print("-" * len(heading), file=out)
    width = max((len(name) for name, _ in rows), default=0)
    for name, value in rows:
        print(f"  {name.ljust(width)}  {value}", file=out)
