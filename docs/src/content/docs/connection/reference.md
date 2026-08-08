---
title: Modbus Connection reference
description: Every class, method, field, function, and exception of the connection layer — ModbusConnection, ModbusUnit, the parameter dataclasses, encode/decode, and the error hierarchy.
---

The complete API of the connection layer. Everything here is importable from the
top-level `modbus_connection` package unless stated otherwise.

## `ModbusConnection`

The abstract connection base class (`modbus_connection.ModbusConnection`). Each
backend module exports a concrete subclass under the same name —
`modbus_connection.tmodbus.ModbusConnection` and
`modbus_connection.pymodbus.ModbusConnection` — with an identical constructor
and API, so selecting a backend changes only the import.

```python
ModbusConnection(params, *, timeout=10, message_spacing=0.0, connect_delay=0.0)
```

| Parameter | Type | Meaning |
| --- | --- | --- |
| `params` | `ModbusTcpParams \| ModbusUdpParams \| ModbusTlsParams \| ModbusSerialParams` | The transport to connect over — see [the parameter dataclasses](#parameter-dataclasses). |
| `timeout` | `float`, default `10` | Per-request timeout in seconds. |
| `message_spacing` | `float`, default `0.0` | Connection-wide minimum interval, in seconds, from the completion of one request to the start of the next. `0` disables spacing. Raises `ValueError` if negative. |
| `connect_delay` | `float`, default `0.0` | Pause, in seconds, after the link is established before it is used. For devices that need a moment after connecting before they answer reliably. Concurrent connectors share one pause. |

Constructing a connection performs no I/O; the first unit operation connects on
demand. See [Connections and units](/modbus-connection/connection/connections-and-units/)
for the ownership and lifecycle model.

### Properties and methods

#### `connected`

`bool` — whether the link is currently established. `False` before the first
request, after a drop, and after `close()`.

#### `connect()`

`async` — establish the connection eagerly; a no-op if already connected.
Concurrent callers share a single in-flight connect attempt. Raises
[`ModbusConnectionError`](#modbusconnectionerror) if the connection fails and
[`ClientClosedError`](#clientclosederror) if the connection was closed. You
rarely need it: every unit operation connects first.

#### `for_unit(unit_id)`

Return this backend's stateless [`ModbusUnit`](#modbusunit) handle bound to
`unit_id`. Handles are cheap; consumers should receive a handle, never the
connection.

#### `on_connection_lost(callback)`

Register a `Callable[[], None]` fired when the link drops; returns an
unsubscribe callable. A connection is **lost** when the transport takes it
away; `close()` and `disconnect()` are the owner tearing it down, so neither
fires the callbacks.

#### `disconnect()`

`async` — drop the link; the next request establishes a new one. For recycling
a link that is up but unusable — a peer that keeps the socket open but stops
answering. Unlike `close()`, the connection stays usable: existing unit handles
and components reconnect on their next request. A no-op when there is no link.
Raises [`ModbusConnectionError`](#modbusconnectionerror) if tearing the old
link down fails; the link is dropped regardless.

#### `close()`

`async` — close the connection permanently. After `close()`, `connect()` and
every unit operation raise [`ClientClosedError`](#clientclosederror); construct
a new connection to reconnect.

## Parameter dataclasses

All four are frozen, keyword-only dataclasses importable from
`modbus_connection`. See
[Choosing connection parameters](/modbus-connection/connection/connections-and-units/#choosing-connection-parameters)
for usage guidance.

### `ModbusTcpParams`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `host` | `str` | required | Host name or IP address of the device. |
| `port` | `int` | `502` | TCP port. |
| `framer` | `"socket" \| "rtu" \| "ascii"` | `"socket"` | Wire framing: native Modbus TCP (MBAP), RTU-over-TCP, or ASCII-over-TCP. Any other value raises `ValueError`. |

### `ModbusUdpParams`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `host` | `str` | required | Host name or IP address of the device. |
| `port` | `int` | `502` | UDP port. |
| `framer` | `"socket" \| "rtu" \| "ascii"` | `"socket"` | Wire framing. Any other value raises `ValueError`. |

### `ModbusTlsParams`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `host` | `str` | required | Host name or IP address of the device. |
| `port` | `int` | `802` | TLS port. |
| `verify` | `bool \| str` | `True` | `True` verifies the server certificate against the system trust store; a path verifies against a private CA file or directory; `False` disables verification. |
| `check_hostname` | `bool` | `True` | Whether to verify the certificate hostname. |
| `client_cert` | `str \| None` | `None` | Path to the client certificate. |
| `client_key` | `str \| None` | `None` | Path to the private key belonging to `client_cert`. |
| `client_key_password` | `str \| None` | `None` | Password for `client_key`, if it is encrypted. |
| `sslctx` | `ssl.SSLContext \| None` | `None` | TLS context overriding the other TLS options. |

#### `create_ssl_context()`

`async` — return the supplied `sslctx` or build an `ssl.SSLContext` from the
other parameters. The backends call this for you when connecting.

### `ModbusSerialParams`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `device` | `str` | required | Serial port device path (e.g. `/dev/ttyUSB0`). |
| `baudrate` | `int` | `9600` | Line speed in baud. |
| `bytesize` | `7 \| 8` | `8` | Data bits per character. |
| `parity` | `"N" \| "E" \| "O"` | `"N"` | Parity: none, even, or odd. |
| `stopbits` | `1 \| 2` | `1` | Stop bits per character. |
| `framer` | `"rtu" \| "ascii"` | `"rtu"` | Serial framing. Any other value raises `ValueError`. |

### `endpoint`

Every parameter dataclass has an `endpoint` property: a hashable tuple
identifying the physical target the params point at, excluding link settings.
Two params objects with equal endpoints address the same device, so the
property answers "do these configurations target the same device?" and doubles
as a dictionary key for grouping shared connections:

| Class | Endpoint | Excluded settings |
| --- | --- | --- |
| `ModbusTcpParams` | `("tcp", host, port)` | `framer` |
| `ModbusUdpParams` | `("udp", host, port)` | `framer` |
| `ModbusTlsParams` | `("tcp", host, port)` | all TLS options |
| `ModbusSerialParams` | `("serial", device)` | `baudrate`, `bytesize`, `parity`, `stopbits`, `framer` |

`ModbusTlsParams` deliberately shares the `"tcp"` transport tag: a TLS link and
a plain-TCP link to the same host and port target the same TCP endpoint, hence
the same device. The host is lowercased in the tuple, since DNS names and IPv6
hex digits are case-insensitive. The serial device path is compared verbatim —
aliases of the same port (a `/dev/serial/by-id` symlink versus `/dev/ttyUSB0`)
are not resolved.

Equal endpoints with **unequal params** signal conflicting configurations for
one device — for example two serial configs for `/dev/ttyUSB0` at different
baud rates — which a connection manager can detect and reject:

```python
if new_params.endpoint == existing_params.endpoint and new_params != existing_params:
    raise ValueError("conflicting settings for the same device")
```

## `ModbusUnit`

A runtime-checkable `Protocol` representing one unit on a shared connection.
Obtain one from [`connection.for_unit(unit_id)`](#for_unitunit_id); the
[mock unit](/modbus-connection/patterns/testing/) and any other object with
these methods satisfies it too.

Every operation is `async`, connects on demand, and raises a subclass of
[`ModbusError`](#modbuserror) on failure. An operation the selected backend does
not implement raises `NotImplementedError` — see
[Choosing a backend](/modbus-connection/getting-started/backends/).

### Register I/O

| Method | Function code | Returns |
| --- | --- | --- |
| `read_holding_registers(address, count)` | 3 (0x03) | `list[int]` |
| `read_input_registers(address, count)` | 4 (0x04) | `list[int]` |
| `write_register(address, value)` | 6 (0x06) | `None` |
| `write_registers(address, values)` | 16 (0x10) | `None` |

### Coil and discrete-input I/O

| Method | Function code | Returns |
| --- | --- | --- |
| `read_coils(address, count)` | 1 (0x01) | `list[bool]` |
| `read_discrete_inputs(address, count)` | 2 (0x02) | `list[bool]` |
| `write_coil(address, value)` | 5 (0x05) | `None` |
| `write_coils(address, values)` | 15 (0x0F) | `None` |

### Diagnostic, file-record, and identification operations

| Method | Function code | Returns |
| --- | --- | --- |
| `read_exception_status()` | 7 (0x07) | `int` |
| `diagnostics(sub_function, data=0)` | 8 (0x08) | `int` |
| `get_comm_event_counter()` | 11 (0x0B) | `tuple[int, int]` — status, event count |
| `get_comm_event_log()` | 12 (0x0C) | `bytes` |
| `report_server_id()` | 17 (0x11) | `bytes` |
| `read_file_record(file, record, length)` | 20 (0x14) | `list[int]` |
| `write_file_record(file, record, values)` | 21 (0x15) | `None` |
| `mask_write_register(address, and_mask, or_mask)` | 22 (0x16) | `None` |
| `read_write_registers(read_address, read_count, write_address, write_values)` | 23 (0x17) | `list[int]` |
| `read_fifo_queue(address)` | 24 (0x18) | `list[int]` |
| `read_device_identification()` | 43 / 14 (0x2B / 0x0E) | `dict[int, bytes]` |

### Properties and non-I/O methods

#### `connected`

`bool` — whether the owning connection's link is currently established.

#### `set_message_spacing(seconds)`

Set the minimum interval between requests to this unit. The setting belongs to
the unit ID and combines with connection-wide spacing by waiting for the longer
interval; pass `0` to clear it. Raises `ValueError` if `seconds` is negative.
See [Request spacing](/modbus-connection/connection/connections-and-units/#request-spacing).

#### `on_connection_lost(callback)`

Register a callback fired when the connection's link drops; returns an
unsubscribe callable. Equivalent to registering on the owning connection.

## Encoding and decoding functions

Converters between register words and Python values, used by the
[modelling fields](/modbus-connection/modelling/fields/) and available for
direct use — see
[Decoding what you read](/modbus-connection/connection/operations/#decoding-what-you-read)
for examples.

### `WordOrder`

`Literal["big", "little"]` (importable from `modbus_connection`) — the order of
16-bit registers within a multi-register value. `"big"` (the common Modbus
convention) puts the most-significant word first; `"little"` puts the
least-significant word first.

### `modbus_connection.decode`

All decoders take `words: list[int]`; the multi-word numeric ones also take a
`word_order` keyword (default `"big"`).

| Function | Registers | Returns |
| --- | --- | --- |
| `decode_uint16(words)` | 1 | `int` (unsigned) |
| `decode_int16(words)` | 1 | `int` (signed) |
| `decode_uint32(words, *, word_order="big")` | 2 | `int` |
| `decode_int32(words, *, word_order="big")` | 2 | `int` |
| `decode_uint64(words, *, word_order="big")` | 4 | `int` |
| `decode_int64(words, *, word_order="big")` | 4 | `int` |
| `decode_int(words, *, signed, word_order="big")` | any | `int` of any width |
| `decode_float32(words, *, word_order="big")` | 2 | `float` (IEEE-754 single) |
| `decode_float64(words, *, word_order="big")` | 4 | `float` (IEEE-754 double) |
| `decode_string(words)` | any | `str` — null-padded ASCII, two characters per word |
| `decode_ipaddr(words)` | 2 | `ipaddress.IPv4Address` |
| `decode_ipv6addr(words)` | 8 | `ipaddress.IPv6Address` |
| `decode_eui48(words)` | 3 | `str` — colon-separated EUI-48 / MAC address |
| `combine_words(words, *, word_order="big")` | any | `int` — the raw unsigned value |

### `modbus_connection.encode`

All encoders return `list[int]` register words. The integer encoders raise
`OverflowError` if the value does not fit the width.

| Function | Registers | Encodes |
| --- | --- | --- |
| `encode_uint16(value)` | 1 | an unsigned/signed 16-bit integer |
| `encode_int16(value)` | 1 | a signed 16-bit integer |
| `encode_uint32(value, *, word_order="big")` | 2 | a 32-bit integer |
| `encode_int32(value, *, word_order="big")` | 2 | a signed 32-bit integer |
| `encode_uint64(value, *, word_order="big")` | 4 | a 64-bit integer |
| `encode_int64(value, *, word_order="big")` | 4 | a signed 64-bit integer |
| `encode_int(value, *, count, word_order="big")` | `count` | an integer of any width |
| `encode_float32(value, *, word_order="big")` | 2 | an IEEE-754 single-precision float |
| `encode_float64(value, *, word_order="big")` | 4 | an IEEE-754 double-precision float |
| `encode_string(value, *, length)` | `length` | an ASCII string, null-padded (two characters per word) |
| `split_words(raw, *, count, word_order="big")` | `count` | an unsigned integer into raw words |

## Exceptions

Both backends map their errors onto the **same neutral hierarchy**, so
`except ModbusError` catches everything regardless of which backend produced it.
Import them from the top-level package:

```text
ModbusError
├── ModbusConnectionError
│   └── ClientClosedError           (request on a close()d connection)
├── ModbusTimeoutError              (also a builtin TimeoutError)
├── ModbusProtocolError
└── ModbusExceptionError            (.exception_code)
    ├── IllegalFunctionError … GatewayTargetError   (one per standard code)
    └── BlockReadError              (.space, .address, .count) — device-modelling
                                    layer; also typed by its code
```

### `ModbusError`

The base class. Catch it to handle any Modbus failure uniformly:

```python
try:
    values = await unit.read_holding_registers(0, 10)
except ModbusError as err:
    log.warning("read failed: %s", err)
```

### `ModbusConnectionError`

The link is down, not connected, or the transport failed. The connection is not
discarded: the next request attempts to establish it again.

### `ClientClosedError`

A request or `connect()` was attempted on a connection after its owner called
`close()`. A closed connection never reconnects; the owner must construct a new
one.

### `ModbusTimeoutError`

An operation timed out: a request got no valid response in time, or a connect
attempt did not complete in time. It also subclasses the builtin `TimeoutError`,
so `except TimeoutError` catches it too:

```python
try:
    await unit.read_holding_registers(0, 1)
except TimeoutError:  # catches ModbusTimeoutError
    ...
```

### `ModbusProtocolError`

A reply arrived but **could not be used** — a corrupt frame (bad CRC/LRC,
framing), or a well-formed answer to a different request than the one sent
(the signature of a bridge shared by several simultaneous clients). The
backends cannot tell the two apart today, so both surface as this one class.

### `ModbusExceptionError`

The device returned a Modbus **exception response** — it understood the request
but refused it. A code with a standard meaning raises the matching subclass, so
callers branch without magic numbers:

```python
try:
    await unit.write_register(40, 99)
except IllegalDataValueError:
    ...  # the device rejected the value
except GatewayTargetError:
    ...  # the bridge is fine; the device behind it is not answering
```

| Subclass | Code | Meaning |
| --- | --- | --- |
| `IllegalFunctionError` | 1 | The device does not support the function. |
| `IllegalDataAddressError` | 2 | The device does not serve the address. |
| `IllegalDataValueError` | 3 | The device rejected a value in the request. |
| `ServerDeviceFailureError` | 4 | The device failed performing the request. |
| `AcknowledgeError` | 5 | Accepted, but the device needs time to process. |
| `ServerDeviceBusyError` | 6 | The device is busy; retry later. |
| `MemoryParityError` | 8 | Parity error in the device's memory. |
| `GatewayPathUnavailableError` | 10 | The gateway has no path to the target. |
| `GatewayTargetError` | 11 | The gateway's target device did not respond. |

`.exception_code` carries the code as an `ExceptionCode` `IntEnum` member when
it is a standard one (a plain `int` otherwise), so existing
`err.exception_code == 2` comparisons keep working. An unknown code raises the
base `ModbusExceptionError`. Each subclass constructs with its code implied —
`IllegalDataAddressError()` — which is handy for
[arming the mock](/modbus-connection/patterns/testing/#simulating-a-read-failure).

### `BlockReadError`

Raised by the [device-modelling layer](/modbus-connection/modelling/overview/), not
a backend: when a component's pooled `async_update()` hits a Modbus exception
response on one of its planned block reads, it surfaces as a `BlockReadError`. It
**subclasses** `ModbusExceptionError`, so `except ModbusExceptionError` catches it and
`.exception_code` still says why the device refused the read; it adds `.space`,
`.address`, and `.count` for which block failed. A block read refused with a standard code raises the combined class for that
code — `IllegalDataAddressBlockReadError`, `ServerDeviceBusyBlockReadError`,
and so on, one per row of the [table above](#modbusexceptionerror) — which
subclasses both `BlockReadError` and the matching typed error. So
`except IllegalDataAddressError` catches the block read the device refused
(useful for probing which components a firmware serves), and
`except IllegalDataAddressBlockReadError` catches exactly that combination. See [When a block read
fails](/modbus-connection/modelling/overview/#when-a-block-read-fails).

## Backend modules

`modbus_connection.tmodbus` and `modbus_connection.pymodbus` each export:

- `ModbusConnection` — the concrete connection class described
  [above](#modbusconnection). Constructing one with parameters the backend does
  not support raises `ValueError` (see
  [Choosing a backend](/modbus-connection/getting-started/backends/)).
- `TmodbusConnection` / `PymodbusConnection` and `TmodbusUnit` / `PymodbusUnit`
  — the old backend-specific names, kept for compatibility. New code should
  import `ModbusConnection` and type against the abstract
  `modbus_connection.ModbusConnection` and the `ModbusUnit` Protocol.
- The legacy factories `connect_tcp`, `connect_udp`, `connect_tls`, and
  `connect_serial` — kept for compatibility. Each builds the matching parameter
  dataclass from keyword arguments, also accepts the constructor's `timeout`,
  `message_spacing`, and `connect_delay`, constructs a `ModbusConnection`, eagerly `connect()`s
  it, and returns it. New code should construct `ModbusConnection` with a
  shared parameter object instead.
