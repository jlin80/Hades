"""Feature computation handler — reacts to token discovery.

Subscribing to the Scanner's ``TokenDiscovered`` domain event is the sanctioned
cross-context coupling (react to events, never call in). On each discovery it
assembles the inputs bundle and asks the Feature Engine to compute + store the
vector. This is the "features" stage of the acquisition pipeline, kept decoupled.

It is also where the **end-to-end** ingest clock stops. The Scanner's own
``analysis_seconds`` starts when a candidate leaves the pipeline queue and stops
at ``TokenDiscovered``, which is the *fastest* leg of the journey; measuring a
change against it would show a gain from work that changed nothing. The figure
that matters — discovered → features ready — spans two contexts, so it can only
be closed here, against the source event's own ``occurred_at``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from hades.contexts.features.application.feature_engine import FeatureEngine
from hades.contexts.features.domain.ports import FeatureInputsAssembler
from hades.contexts.scanner.domain.events import TokenDiscovered
from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.events import EventBus
from hades.shared_kernel.logging import get_logger

_logger = get_logger("features.subscriber")

#: Called with the seconds elapsed from ``TokenDiscovered`` to features ready.
LatencySink = Callable[[float], None]


class FeatureComputationHandler:
    """Computes features whenever a token is discovered."""

    def __init__(
        self,
        engine: FeatureEngine,
        assembler: FeatureInputsAssembler,
        *,
        on_latency: LatencySink | None = None,
    ) -> None:
        self._engine = engine
        self._assembler = assembler
        self._on_latency = on_latency

    def register(self, event_bus: EventBus) -> None:
        event_bus.subscribe(TokenDiscovered.__name__, self.handle)

    async def handle(self, event: DomainEvent) -> None:
        if not isinstance(event, TokenDiscovered):
            return
        try:
            inputs = await self._assembler.build(
                event.token, hint_liquidity=float(event.initial_liquidity.amount)
            )
            await self._engine.compute(inputs)
        except Exception as exc:  # never let one token break the subscriber
            _logger.warning(
                "feature_computation_failed", mint=str(event.token.mint), error=str(exc)
            )
            return
        # Only a token that actually reached a computed feature set counts. A
        # failure recorded as a latency would make the pipeline look faster the
        # more often it broke.
        self._observe_latency(event)

    def _observe_latency(self, event: TokenDiscovered) -> None:
        if self._on_latency is None:
            return
        occurred = event.occurred_at
        reference = occurred if occurred.tzinfo is not None else occurred.replace(tzinfo=UTC)
        elapsed = (datetime.now(UTC) - reference).total_seconds()
        if elapsed < 0:
            return
        _logger.info(
            "features_ready",
            mint=str(event.token.mint),
            source=event.source,
            discovered_to_features_seconds=round(elapsed, 4),
        )
        try:
            self._on_latency(elapsed)
        except Exception as exc:  # telemetry must never break the pipeline
            _logger.debug("features_latency_sink_failed", error=str(exc))
