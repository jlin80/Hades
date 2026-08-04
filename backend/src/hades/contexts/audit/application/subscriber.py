"""AuditSubscriber — turns consequential domain events into audit entries.

This is the non-invasive half of the Audit System: instead of editing every
context to call the recorder, we subscribe to the events those contexts *already*
publish and translate each into an :class:`AuditEntry`. One generic handler works
for any event because the envelope already carries everything an entry needs —
``aggregate_type`` (entity type), ``aggregate_id`` (entity id) and the payload
(the "after" snapshot). An optional ``actor`` in the payload is honoured.

We deliberately do **not** subscribe to ``TradingModeChanged``: the trading-mode
switch is audited at its source (``TradingModeRepository``) with full before/after
mode, so subscribing here would double-record it. Configuration edits are audited
directly by the Configuration Manager via the :class:`AuditTrail` port. Everything
else consequential — promotions, weight changes, the whole risk defence layer —
flows through here.
"""

from __future__ import annotations

from hades.contexts.audit.application.recorder import AuditRecorder
from hades.contexts.audit.domain.models import AuditEntry
from hades.contexts.learning.domain.events import (
    ModelPromoted,
    ModelPromotionProposed,
    ModelRejected,
)
from hades.contexts.research.domain.events import (
    PromotionRejected,
    ResearchStrategyPromoted,
)
from hades.contexts.risk.domain.events import (
    CircuitBreakerReset,
    CircuitBreakerTripped,
    EmergencyModeEntered,
    EmergencyModeExited,
    KillSwitchEngaged,
    KillSwitchLevelChanged,
    KillSwitchReset,
    RiskControlCommandIssued,
)
from hades.contexts.strategy.domain.events import (
    StrategyDisabled,
    StrategyPromoted,
    WeightUpdated,
)
from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.events import EventBus
from hades.shared_kernel.logging import get_logger

_logger = get_logger("audit.subscriber")

# Event class → audit action label. Every event here is already registered on the
# bus (see ``bootstrap._build_registry``); we only observe, never emit.
_AUDITED: dict[type[DomainEvent], str] = {
    ModelPromoted: "model_promoted",
    ModelPromotionProposed: "model_promotion_proposed",
    ModelRejected: "model_rejected",
    ResearchStrategyPromoted: "research_strategy_promoted",
    PromotionRejected: "research_promotion_rejected",
    StrategyPromoted: "strategy_promoted",
    WeightUpdated: "strategy_weight_updated",
    StrategyDisabled: "strategy_disabled",
    KillSwitchLevelChanged: "kill_switch_level_changed",
    KillSwitchEngaged: "kill_switch_engaged",
    KillSwitchReset: "kill_switch_reset",
    CircuitBreakerTripped: "circuit_breaker_tripped",
    CircuitBreakerReset: "circuit_breaker_reset",
    EmergencyModeEntered: "emergency_mode_entered",
    EmergencyModeExited: "emergency_mode_exited",
    RiskControlCommandIssued: "risk_control_command",
}


class AuditSubscriber:
    """Subscribes to consequential events and records them, one entry each."""

    def __init__(self, recorder: AuditRecorder) -> None:
        self._recorder = recorder
        self._actions = {cls.__name__: action for cls, action in _AUDITED.items()}

    def register(self, event_bus: EventBus) -> None:
        for event_cls in _AUDITED:
            event_bus.subscribe(event_cls.__name__, self.handle)

    async def handle(self, event: DomainEvent) -> None:
        action = self._actions.get(event.event_type)
        if action is None:
            return
        try:
            envelope = event.to_envelope()
            payload = envelope["payload"]
            actor = self._actor_of(payload)
            await self._recorder.record(
                AuditEntry(
                    actor=actor,
                    action=action,
                    entity_type=str(envelope["aggregate_type"]),
                    entity_id=str(envelope["aggregate_id"]),
                    after=payload if isinstance(payload, dict) else {"value": payload},
                    occurred_at=event.occurred_at,
                )
            )
        except Exception as exc:  # one event must never break the subscriber
            _logger.warning("audit_event_skipped", event_type=event.event_type, error=str(exc))

    @staticmethod
    def _actor_of(payload: object) -> str:
        if isinstance(payload, dict):
            actor = payload.get("actor")
            if isinstance(actor, str) and actor:
                return actor
        return "system"
