"""Domain events emitted by the Risk Manager - the guardian's audit trail.

Every consequential act of the Risk Manager is a fact on the bus: a trade
approved (with its sizing envelope) or rejected (with its cause), risk being
reduced, a Kill Switch level changing, the Circuit Breaker tripping, Emergency
Mode being entered. They are append-only and carry enough payload for the
dashboard, the audit log and the AI Committee's Knowledge Feedback - but a
:class:`TradeApproved` is a *permission*, never an executed order. Only the
Execution Engine (a later phase) turns it into a real trade.
"""

from __future__ import annotations

from hades.contexts.common.domain.value_objects import TokenRef
from hades.contexts.risk.domain.models import (
    CircuitBreakerReason,
    PositionSizing,
    RiskRejectReason,
)
from hades.shared_kernel.domain.events import DomainEvent

# --- trade-decision events ---------------------------------------------------


class TradeApproved(DomainEvent):
    """Risk approved a sized trade. The Execution Engine acts on this.

    Carries the full sizing envelope and the human-readable rationale. The tags
    (``strategy``/``developer``/``cluster``/``narrative``/``regime``) travel with
    the approval so the opened position can be attributed for exposure tracking.

    ``exploration`` marks a trade taken under the cold-start exploration
    programme: a fixed, tiny size charged to an independent budget, bought to
    generate evidence rather than for its expected return. It travels here so it
    reaches the position, the fill and — the reason it exists — the settled
    lesson, without which permanent memory would hold the trade but not the fact
    that the platform paid for it in order to learn.
    """

    aggregate_type: str = "token"
    token: TokenRef
    sizing: PositionSizing
    conviction: float
    risk_amount_usd: float
    rationale: str
    strategy: str = "default"
    developer: str | None = None
    cluster: str | None = None
    narrative: str | None = None
    regime: str = "unknown"
    exploration: bool = False
    correlation_id_ref: str | None = None


class TradeRejected(DomainEvent):
    """Risk declined a candidate. Recorded for learning + audit."""

    aggregate_type: str = "token"
    token: TokenRef
    reason: RiskRejectReason
    detail: str = ""
    strategy: str = "default"
    correlation_id_ref: str | None = None


# --- defence-layer events ----------------------------------------------------


class RiskReduced(DomainEvent):
    """The Kill Switch or a policy shrank the risk envelope (size factor < 1)."""

    aggregate_type: str = "risk_manager"
    reason: str
    size_factor: float
    consecutive_losses: int = 0


class KillSwitchLevelChanged(DomainEvent):
    """The graduated Kill Switch moved to a new level (up or down)."""

    aggregate_type: str = "risk_manager"
    previous_level: int
    level: int
    label: str
    reason: str


class KillSwitchEngaged(DomainEvent):
    """Global trading halt tripped (loss limit, anomaly, manual)."""

    aggregate_type: str = "risk_manager"
    level: int
    reason: str


class KillSwitchReset(DomainEvent):
    """The Kill Switch was cleared back to normal operation."""

    aggregate_type: str = "risk_manager"
    reason: str = "manual reset"


class CircuitBreakerTripped(DomainEvent):
    """Trading was halted because a condition made it unsafe to operate."""

    aggregate_type: str = "risk_manager"
    reasons: tuple[CircuitBreakerReason, ...]
    detail: str = ""


class CircuitBreakerReset(DomainEvent):
    """The Circuit Breaker closed - conditions returned to safe."""

    aggregate_type: str = "risk_manager"
    detail: str = ""


class EmergencyModeEntered(DomainEvent):
    """Emergency: block all new entries, flag positions for close, notify."""

    aggregate_type: str = "risk_manager"
    reason: str


class EmergencyModeExited(DomainEvent):
    """Emergency mode was lifted (human action)."""

    aggregate_type: str = "risk_manager"
    reason: str = "manual"


class DrawdownLimitBreached(DomainEvent):
    """A rolling loss ceiling (daily/weekly/monthly) was exceeded."""

    aggregate_type: str = "risk_manager"
    window: str  # "daily" | "weekly" | "monthly"
    loss_pct: float
    limit_pct: float


class ExposureLimitBreached(DomainEvent):
    """A concentration cap on some axis was hit and the trade was blocked."""

    aggregate_type: str = "token"
    token: TokenRef
    axis: str
    key: str
    exposure_pct: float
    limit_pct: float


class RiskControlCommandIssued(DomainEvent):
    """An operator command targeting the defence layer, issued via the API.

    The API process does not own the running Risk Manager (the Worker does), so a
    human action - resetting the Kill Switch, closing the Circuit Breaker,
    entering/leaving Emergency Mode - travels to the Worker as this event. It is
    auditable like everything else. ``action`` is one of the ``ACTION_*`` values.
    """

    aggregate_type: str = "risk_manager"
    action: str
    reason: str = ""


#: Valid ``RiskControlCommandIssued.action`` values.
ACTION_RESET_KILL_SWITCH = "reset_kill_switch"
ACTION_RESET_CIRCUIT_BREAKER = "reset_circuit_breaker"
ACTION_ENTER_EMERGENCY = "enter_emergency"
ACTION_EXIT_EMERGENCY = "exit_emergency"
ACTION_TRIP_CIRCUIT_BREAKER = "trip_circuit_breaker"
