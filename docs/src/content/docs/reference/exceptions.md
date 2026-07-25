---
title: Exceptions
description: The neutral exception hierarchy every backend maps its errors onto.
---

Both backends map their errors onto the **same neutral hierarchy**, so
`except ModbusError` catches everything regardless of which backend produced it.
Every `ModbusUnit` method raises on failure — it never returns `None`.

```text
ModbusError
├── ModbusConnectionError
│   └── ClientClosedError     (request on a close()d connection)
├── ModbusTimeoutError        (also a builtin TimeoutError)
├── ModbusProtocolError
└── ModbusExceptionError      (.exception_code)
    └── BlockReadError        (.space, .address, .count) — device-modelling layer
```

Import them from the top-level package:

```python
from modbus_connection import (
    ModbusError,
    ModbusConnectionError,
    ClientClosedError,
    ModbusTimeoutError,
    ModbusExceptionError,
    ModbusProtocolError,
    BlockReadError,
)
```

## The hierarchy

### `ModbusError`

The base class. Catch it to handle any Modbus failure uniformly:

```python
try:
    values = await unit.read_holding_registers(0, 10)
except ModbusError as err:
    log.warning("read failed: %s", err)
```

### `ModbusConnectionError`

The link is down, not connected, or the transport failed. A `ModbusConnection`
re-establishes a dropped link on the next request, so this surfaces when the
(re)connect attempt itself fails — a sleeping device is a failed poll, not a
teardown.

### `ClientClosedError`

A request was attempted on a connection after its owner called `close()`. A
closed connection never reconnects; the owner must construct a new one. It
subclasses `ModbusConnectionError`, so existing handlers keep catching it.

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

### `ModbusExceptionError`

The device returned a Modbus **exception response** — it understood the request
but refused it (illegal address, illegal value, and so on). The raw code is on
`.exception_code`:

```python
try:
    await unit.write_register(40, 99)
except ModbusExceptionError as err:
    if err.exception_code == 3:  # illegal data value
        ...
```

One code is handled for you: `SERVER_DEVICE_BUSY` (0x06) means the device did
not execute the request and asks for a retransmission, so units retry it
automatically with a short bounded backoff and only raise if the device stays
busy.

### `BlockReadError`

Raised by the [device-modelling layer](/modbus-connection/modelling/overview/), not
a backend: when a component's pooled `async_update()` hits a Modbus exception
response on one of its planned block reads, it surfaces as a `BlockReadError`. It
**subclasses** `ModbusExceptionError`, so `except ModbusExceptionError` catches it and
`.exception_code` still says why the device refused the read; it adds `.space`,
`.address`, and `.count` for which block failed. See [When a block read
fails](/modbus-connection/modelling/overview/#when-a-block-read-fails).

### `ModbusProtocolError`

A reply arrived but was **not a valid frame** — bad CRC/LRC, framing, or a
mismatched header. Only backends that can tell a garbled reply from a missing one
raise it: **tmodbus does**; **pymodbus cannot**, and surfaces both a garbled and a
missing reply as `ModbusTimeoutError`.

## Backend differences

| Situation | pymodbus | tmodbus |
| --- | --- | --- |
| No response in time | `ModbusTimeoutError` | `ModbusTimeoutError` |
| Garbled / corrupt reply | `ModbusTimeoutError` | `ModbusProtocolError` |
| Device exception response | `ModbusExceptionError` | `ModbusExceptionError` |
| Link down | `ModbusConnectionError` | `ModbusConnectionError` |

If you need to distinguish a corrupt reply from a silent timeout, use tmodbus. If
you catch `ModbusError` (or `ModbusTimeoutError` plus `ModbusExceptionError`) your
code is correct on both.

## Not-implemented function codes

A backend that can't implement a given Modbus function code raises the builtin
`NotImplementedError` (not part of the `ModbusError` tree). tmodbus does this for
diagnostics and the comm-event codes, and for the UDP / ASCII-over-TCP transports.
