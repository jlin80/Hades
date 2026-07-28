"""Rendering an exception for a log line.

``str(exc)`` is the obvious way to log a failure and it is lossy in exactly the
cases that matter most. Several of the exceptions this platform meets on its hot
paths carry no message at all — ``httpx.ConnectTimeout``, ``httpx.ReadTimeout``,
``httpx.ConnectError`` and ``asyncio.TimeoutError`` among them — so a handler
that logs ``error=str(exc)`` emits ``error=""`` and destroys the only evidence
there was. A production incident produced pages of

    {"source": "raydium", "error": "", "event": "discovery_source_error"}

which cannot distinguish a DNS failure from a connection timeout from a refused
connection, and the type name alone would have separated all three.

The type name is always present, so lead with it.
"""

from __future__ import annotations


def describe(exc: BaseException) -> str:
    """``"TypeName: message"``, or just ``"TypeName"`` when there is no message.

    Never raises: a logging helper that can fail turns a diagnosable error into
    two undiagnosable ones.
    """
    name = type(exc).__name__
    try:
        message = str(exc).strip()
    except Exception:  # a __str__ that raises must not break the log line
        return name
    return f"{name}: {message}" if message else name
