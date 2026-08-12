"""The funnel must cost one computation, however many pollers ask for it.

These pin the property the CT incident was about, not the caching mechanism:
six concurrent requests were observed each running their own 36 s query, so the
regression to prevent is *concurrent callers multiplying the work*. A test that
only checked a warm cache would pass against the broken version, because the
broken version was never wrong once warm — it was wrong while cold and busy.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from hades.api.routers.funnel import FUNNEL_CACHE_TTL_SECONDS, _SingleFlightCache


class _Counter:
    """A computation that records how often it actually ran."""

    def __init__(self, delay: float = 0.0) -> None:
        self.calls = 0
        self._delay = delay

    async def __call__(self) -> dict[str, Any]:
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        return {"calls": self.calls}


@pytest.mark.asyncio
async def test_concurrent_callers_share_one_computation() -> None:
    """Ten simultaneous pollers against a cold cache run the query once."""
    cache = _SingleFlightCache(ttl_seconds=FUNNEL_CACHE_TTL_SECONDS)
    compute = _Counter(delay=0.05)

    results = await asyncio.gather(*(cache.get(24, compute) for _ in range(10)))

    assert compute.calls == 1, "a cold cache let concurrent pollers stack queries"
    assert all(r == {"calls": 1} for r in results), "waiters must get the winner's answer"


@pytest.mark.asyncio
async def test_result_is_reused_until_the_ttl_expires() -> None:
    cache = _SingleFlightCache(ttl_seconds=60.0)
    compute = _Counter()

    await cache.get(24, compute)
    await cache.get(24, compute)

    assert compute.calls == 1


@pytest.mark.asyncio
async def test_expired_result_is_recomputed() -> None:
    cache = _SingleFlightCache(ttl_seconds=-1.0)  # everything is already stale
    compute = _Counter()

    await cache.get(24, compute)
    await cache.get(24, compute)

    assert compute.calls == 2


@pytest.mark.asyncio
async def test_windows_are_cached_independently() -> None:
    """A 1-hour funnel must not be served the 24-hour answer."""
    cache = _SingleFlightCache(ttl_seconds=60.0)

    async def compute_for(hours: int) -> dict[str, Any]:
        return {"window_hours": hours}

    first = await cache.get(1, lambda: compute_for(1))
    second = await cache.get(24, lambda: compute_for(24))

    assert first == {"window_hours": 1}
    assert second == {"window_hours": 24}


@pytest.mark.asyncio
async def test_failures_are_not_cached() -> None:
    """A hiccup must not be replayed for a minute on the diagnostic endpoint."""
    cache = _SingleFlightCache(ttl_seconds=60.0)
    attempts = 0

    async def flaky() -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("database hiccup")
        return {"ok": True}

    with pytest.raises(RuntimeError):
        await cache.get(24, flaky)

    assert await cache.get(24, flaky) == {"ok": True}
    assert attempts == 2


@pytest.mark.asyncio
async def test_the_endpoint_itself_is_single_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    """The route, not just the cache class, must not multiply the work.

    Deliberately exercises ``funnel()`` rather than ``_SingleFlightCache``: the
    incident was an endpoint that ran its queries per request, and a later edit
    that computed around the cache would leave every test above still green.

    This cannot be run red against the pre-fix version — that version had no
    seam to patch, so the failure it prevents is a future one, not a past one.
    """
    from hades.api.routers import funnel as funnel_module

    funnel_module._cache.clear()
    calls = 0

    async def fake_compute(container: Any, hours: int) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return {"window_hours": hours, "stages": [], "reject_reasons": {}}

    monkeypatch.setattr(funnel_module, "_compute", fake_compute)

    class _Container:
        database = object()  # only checked for None

    container = _Container()
    await asyncio.gather(
        *(funnel_module.funnel(hours=24, container=container) for _ in range(8))  # type: ignore[arg-type]
    )

    assert calls == 1, "the endpoint ran its queries once per request"
    funnel_module._cache.clear()


@pytest.mark.asyncio
async def test_a_failing_computation_releases_the_lock() -> None:
    """A raise inside the lock must not wedge every later caller."""
    cache = _SingleFlightCache(ttl_seconds=60.0)

    async def boom() -> dict[str, Any]:
        raise RuntimeError("boom")

    for _ in range(3):
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(cache.get(24, boom), timeout=1.0)
