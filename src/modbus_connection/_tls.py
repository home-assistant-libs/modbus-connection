"""Shared TLS helpers for the backend ``connect_tls`` functions."""

from __future__ import annotations

import os
import ssl


def build_tls_context(
    verify: bool | str,
    check_hostname: bool,
    client_cert: str | None,
    client_key: str | None,
    client_key_password: str | None,
) -> ssl.SSLContext:
    """Build a client TLS context for ``connect_tls`` (blocking; run in a thread).

    Both backends share this so they produce identical contexts from the same
    arguments.

    ``verify`` / ``check_hostname`` are the *server* side (see ``connect_tls``);
    ``client_cert`` / ``client_key`` / ``client_key_password`` are the *client*
    certificate this side presents for mutual TLS, applied independently.

    Reads the system trust store and any cert files from disk, so callers offload
    it with :func:`asyncio.to_thread`.
    """
    if isinstance(verify, str):
        if os.path.isdir(verify):
            context = ssl.create_default_context(capath=verify)
        else:
            context = ssl.create_default_context(cafile=verify)
    else:
        context = ssl.create_default_context()
        if not verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
    if verify and not check_hostname:
        context.check_hostname = False  # still verifies the cert, skips the name
    if client_cert is not None:
        context.load_cert_chain(client_cert, client_key, client_key_password)
    return context
