"""A shared, long-lived HTTP client for short outbound probes and lookups.

Opening an ``httpx.AsyncClient`` per call is the default thing to reach for and
it is expensive in a way that hides: each call pays DNS, a TCP handshake and a
full TLS negotiation before the request even starts. Measured against a real
Solana RPC provider from the deployment host, the same ``getHealth`` took ~270ms
with a fresh client and ~30ms once the connection was reused — and far worse on
a loaded host, where the TLS handshake competes for CPU with everything else.

For the health probes that mattered twice over: the probe *reports* the latency
it measures, so it was attributing its own connection setup to the dependency
and could cross the "degraded" threshold while the dependency was perfectly
healthy.

This provider lives on the :class:`~hades.bootstrap.Container` because callers
like the probes are rebuilt per request; the connection pool has to outlive
them. Any failure discards the client so the next caller builds a fresh one,
which covers both a broken connection and the subtler case of the client being
reused from a different event loop.
"""

from __future__ import annotations

import contextlib

import httpx


class HttpClientProvider:
    """Owns one reused ``httpx`` client and its lifecycle."""

    def __init__(self, *, timeout_seconds: float = 15.0) -> None:
        self._timeout = timeout_seconds
        self._client: httpx.AsyncClient | None = None

    @property
    def timeout_seconds(self) -> float:
        return self._timeout

    def acquire(self, *, timeout_seconds: float | None = None) -> httpx.AsyncClient:
        """Return the shared client, creating it on first use."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=timeout_seconds or self._timeout)
        return self._client

    def discard(self) -> None:
        """Drop the client without awaiting; the next acquire rebuilds it.

        Used from an error path, where awaiting a close on an already-broken
        connection is both pointless and a second chance to raise.
        """
        self._client = None

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None and not client.is_closed:
            with contextlib.suppress(Exception):
                await client.aclose()
