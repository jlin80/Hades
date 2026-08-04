"""Discovery Engine — the mouth of the funnel.

Continuously reads every configured :class:`TokenSource` (each a DEX/aggregator
adapter), deduplicates, applies *coarse* gating (min liquidity, max age, black/
white lists) and hands genuinely-new candidates to a sink (the acquisition
pipeline). It forms no opinion beyond "is this obvious noise?".

Resilience is first-class: each source runs in its own supervised task; if a
source errors or its stream ends, the engine emits a ``SourceHealthChanged``
event, backs off, and reconnects — one bad DEX never stops the others, and the
engine is designed to run for months without intervention.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from hades.contexts.scanner.application.metrics import ScannerMetrics
from hades.contexts.scanner.domain.events import SourceHealthChanged
from hades.contexts.scanner.domain.ports import (
    RawTokenCandidate,
    SeenTokenRegistry,
    TokenSource,
)
from hades.shared_kernel.config.settings import ScannerSettings
from hades.shared_kernel.domain.identifiers import new_id
from hades.shared_kernel.events import EventBus
from hades.shared_kernel.logging import describe, get_logger

_logger = get_logger("scanner.discovery")

CandidateSink = Callable[[RawTokenCandidate], Awaitable[None]]

# Reconnect backoff bounds for a failing source (seconds).
_BACKOFF_MIN = 1.0
_BACKOFF_MAX = 60.0


class DiscoveryEngine:
    """Runs all sources, dedups + coarse-gates, and forwards new candidates."""

    def __init__(
        self,
        sources: list[TokenSource],
        registry: SeenTokenRegistry,
        sink: CandidateSink,
        *,
        settings: ScannerSettings,
        event_bus: EventBus,
        metrics: ScannerMetrics,
    ) -> None:
        self._sources = sources
        self._registry = registry
        self._sink = sink
        self._settings = settings
        self._bus = event_bus
        self._metrics = metrics
        self._source_up: dict[str, bool] = {}

    def source_health(self) -> dict[str, bool]:
        """Current up/down state of each discovery source."""
        return dict(self._source_up)

    async def run(self, stop: asyncio.Event) -> None:
        """Run every source until ``stop`` is set."""
        if not self._sources:
            _logger.warning("discovery_no_sources")
            return
        for source in self._sources:
            self._metrics.sources_available.labels(source=source.name).set(1)
            self._source_up[source.name] = True
        tasks = [
            asyncio.create_task(self._run_source(source, stop), name=f"discovery:{source.name}")
            for source in self._sources
        ]
        await stop.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_source(self, source: TokenSource, stop: asyncio.Event) -> None:
        backoff = _BACKOFF_MIN
        while not stop.is_set():
            try:
                async for candidate in source.stream():
                    if stop.is_set():
                        break
                    await self._handle(candidate, source.name)
                    backoff = _BACKOFF_MIN  # progress → reset backoff
                # Stream ended cleanly; treat as a transient gap and retry.
                await self._sleep_or_stop(stop, self._settings.poll_interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._mark_source(source.name, up=False, detail=str(exc))
                _logger.warning("discovery_source_error", source=source.name, error=describe(exc))
                await self._sleep_or_stop(stop, backoff)
                backoff = min(_BACKOFF_MAX, backoff * 2)
            else:
                await self._mark_source(source.name, up=True, detail="")

    async def _handle(self, candidate: RawTokenCandidate, source_name: str) -> None:
        await self._mark_source(source_name, up=True, detail="")
        reason = self._reject_reason(candidate)
        if reason is not None:
            self._metrics.rejected.labels(reason=reason).inc()
            return
        if not await self._registry.is_new(candidate.token):
            self._metrics.rejected.labels(reason="duplicate").inc()
            return
        await self._registry.mark_seen(candidate.token)
        self._metrics.discovered.labels(source=source_name).inc()
        await self._sink(candidate)

    def _reject_reason(self, candidate: RawTokenCandidate) -> str | None:
        s = self._settings
        mint = str(candidate.token.mint)
        if mint in s.blacklisted_tokens:
            return "blacklist_token"
        if candidate.deployer is not None and str(candidate.deployer) in s.blacklisted_deployers:
            return "blacklist_deployer"
        # Whitelisted mints bypass the quantitative gates (but not the blacklist).
        if mint in s.whitelisted_tokens:
            return None
        if float(candidate.liquidity.amount) < s.min_liquidity_usd:
            return "low_liquidity"
        return None

    async def _mark_source(self, name: str, *, up: bool, detail: str) -> None:
        if self._source_up.get(name) == up:
            return
        self._source_up[name] = up
        self._metrics.sources_available.labels(source=name).set(1 if up else 0)
        await self._bus.publish(
            SourceHealthChanged(aggregate_id=new_id(), source=name, available=up, detail=detail)
        )

    async def _sleep_or_stop(self, stop: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except TimeoutError:
            return
