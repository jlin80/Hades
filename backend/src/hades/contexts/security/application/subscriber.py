"""Security analysis handler — reacts to feature computation.

Subscribing to the Feature Engine's ``FeaturesComputed`` event is the sanctioned
cross-context coupling (react to events, never call in). On each computed vector
it asks the assembler to gather the full analysis context (fresh on-chain reads,
stored facts, honeypot probe, developer reputation, wallet clusters) and hands it
to the :class:`SecurityEngine`. This is the ``features → security`` stage of the
documented flow, kept decoupled.
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

from hades.contexts.features.domain.events import FeaturesComputed
from hades.contexts.security.application.engine import SecurityEngine
from hades.contexts.security.application.metrics import SecurityMetrics
from hades.contexts.security.domain.models import SecurityInputs
from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.events import EventBus
from hades.shared_kernel.logging import get_logger

_logger = get_logger("security.subscriber")


@runtime_checkable
class SecurityInputsAssembler(Protocol):
    """Builds the full analysis context for a token from a features event.

    The concrete assembler (infrastructure) performs all I/O — fresh on-chain
    reads, stored facts, honeypot simulation, developer reputation, clustering —
    so the engine and analyzers stay pure.
    """

    async def build(self, event: FeaturesComputed) -> SecurityInputs: ...


class SecurityAnalysisHandler:
    """Analyses a token's security whenever its features are computed."""

    def __init__(
        self,
        engine: SecurityEngine,
        assembler: SecurityInputsAssembler,
        metrics: SecurityMetrics | None = None,
    ) -> None:
        self._engine = engine
        self._assembler = assembler
        # Optional so every existing construction site keeps working; the runtime
        # passes the same registry the engine already uses.
        self._metrics = metrics

    def register(self, event_bus: EventBus) -> None:
        event_bus.subscribe(FeaturesComputed.__name__, self.handle)

    async def handle(self, event: DomainEvent) -> None:
        if not isinstance(event, FeaturesComputed):
            return
        try:
            started = asyncio.get_running_loop().time()
            inputs = await self._assembler.build(event)
            if self._metrics is not None:
                self._metrics.assemble_seconds.observe(
                    asyncio.get_running_loop().time() - started
                )
            await self._engine.analyze(inputs)
        except Exception as exc:  # never let one token break the subscriber
            _logger.warning(
                "security_analysis_skipped",
                mint=str(event.token.mint),
                error=str(exc),
            )
