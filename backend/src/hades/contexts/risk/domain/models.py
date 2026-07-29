"""The vocabulary of the Risk Manager - every value object it reasons with.

The Risk Manager is the guardian of capital: the *only* component in Hades
authorised to approve or reject a trade. The AI Committee quantifies, the
Scanner discovers, the Security Engine screens - none of them may commit money.
Every value object here is immutable and, like the rest of the platform,
*explainable by construction*: an approval or a rejection always carries the
full chain of reasons behind it (:class:`RiskAssessment`).

Nothing in this module executes anything. A :class:`RiskAssessment` with
``decision == APPROVE`` is a *permission slip* with a sizing envelope attached;
the Execution Engine (a later phase) is what acts on it. The platform rule holds
everywhere: capital is only ever committed after the Risk Manager says yes.

The design mirrors the philosophy the user set out:

* **Survive first, earn second.** Every limit defaults conservative; doubt
  reduces size or rejects, never the opposite.
* **Composable rules.** Approval requires *every* policy to pass; any policy may
  veto. New rules are added without touching existing ones.
* **Layered defence.** A Kill Switch (five graduated levels) and a Circuit
  Breaker sit above the per-trade rules; either can halt the whole book.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import IntEnum, StrEnum

from pydantic import Field

from hades.contexts.common.domain.value_objects import Money, Percentage, TokenRef
from hades.shared_kernel.domain.base import ValueObject

# =============================================================================
# Enums
# =============================================================================


class RiskDecision(StrEnum):
    """The binary verdict of a risk review."""

    APPROVE = "approve"
    REJECT = "reject"


class RiskRejectReason(StrEnum):
    """Why a candidate was declined - one machine-readable cause per rejection.

    Every value maps to exactly one policy so a rejection is always traceable to
    the rule that vetoed it. Ordered roughly from "global halt" down to
    "per-trade sizing", matching the order the chain evaluates.
    """

    # Global halts (checked first).
    KILL_SWITCH_ENGAGED = "kill_switch_engaged"
    CIRCUIT_BREAKER_OPEN = "circuit_breaker_open"
    EMERGENCY_MODE = "emergency_mode"
    TRADING_HALTED = "trading_halted"
    # Signal quality.
    LOW_PROBABILITY = "low_probability"
    LOW_CONFIDENCE = "low_confidence"
    SECURITY_REJECTED = "security_rejected"
    LOW_SECURITY_SCORE = "low_security_score"
    DEVELOPER_RISKY = "developer_risky"
    WALLET_SUSPICIOUS = "wallet_suspicious"
    LIQUIDITY_INSUFFICIENT = "liquidity_insufficient"
    ENSEMBLE_DISAGREES = "ensemble_disagrees"
    # Portfolio-level limits.
    MAX_POSITIONS = "max_positions"
    INSUFFICIENT_CAPITAL = "insufficient_capital"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    WEEKLY_LOSS_LIMIT = "weekly_loss_limit"
    MONTHLY_LOSS_LIMIT = "monthly_loss_limit"
    DAILY_STOP_LIMIT = "daily_stop_limit"
    TRADE_RATE_LIMIT = "trade_rate_limit"
    # Exposure & correlation.
    TOKEN_EXPOSURE = "token_exposure"
    DEVELOPER_EXPOSURE = "developer_exposure"
    CLUSTER_EXPOSURE = "cluster_exposure"
    STRATEGY_EXPOSURE = "strategy_exposure"
    NARRATIVE_EXPOSURE = "narrative_exposure"
    REGIME_EXPOSURE = "regime_exposure"
    PORTFOLIO_EXPOSURE = "portfolio_exposure"
    CORRELATED_POSITIONS = "correlated_positions"
    RISK_BUDGET_EXHAUSTED = "risk_budget_exhausted"
    # Sizing.
    POSITION_TOO_SMALL = "position_too_small"


class KillSwitchLevel(IntEnum):
    """Graduated trading halt. Higher is stricter; each subsumes the ones below.

    The levels are the five the user specified, ordered so numeric comparison is
    meaningful (``level >= PAPER_ONLY`` blocks live commitment).
    """

    NONE = 0  # normal operation
    REDUCED = 1  # 3 consecutive losses -> shrink size
    HALTED = 2  # 5 consecutive losses -> stop new entries for a cooldown
    OBSERVATION = 3  # daily drawdown exceeded -> observe only, no entries
    PAPER_ONLY = 4  # critical drawdown -> paper trading only
    EMERGENCY = 5  # emergency -> stop everything, notify, audit

    @property
    def blocks_entries(self) -> bool:
        """Levels at or above HALTED forbid opening new positions."""
        return self >= KillSwitchLevel.HALTED

    @property
    def label(self) -> str:
        return self.name.lower()


class CircuitBreakerReason(StrEnum):
    """A condition under which it is unsafe to trade at all - trips the breaker."""

    RPC_UNSTABLE = "rpc_unstable"
    HIGH_LATENCY = "high_latency"
    ILLIQUID_MARKET = "illiquid_market"
    REPEATED_ERRORS = "repeated_errors"
    AI_FAILURE = "ai_failure"
    SECURITY_FAILURE = "security_failure"
    DATABASE_FAILURE = "database_failure"
    WALLET_FAILURE = "wallet_failure"
    NOTIFICATION_FAILURE = "notification_failure"
    MANUAL = "manual"


class ExposureAxis(StrEnum):
    """A dimension along which concentration is measured and capped."""

    TOKEN = "token"
    DEVELOPER = "developer"
    CLUSTER = "cluster"
    STRATEGY = "strategy"
    NARRATIVE = "narrative"
    DEX = "dex"
    REGIME = "regime"
    WALLET = "wallet"


class ReasonKind(StrEnum):
    """Whether a legible reason argues for, against, or qualifies a decision."""

    DRIVER = "driver"  # a reason the trade was approved / attractive
    BLOCKER = "blocker"  # a reason it was rejected
    CAVEAT = "caveat"  # a reason to distrust the decision even if approved


# =============================================================================
# Persisted risk envelope (approved sizing)
# =============================================================================


class PositionSizing(ValueObject):
    """The size + risk envelope the Risk Manager approves for a trade.

    Take-profit / stop-loss / trailing are risk parameters attached at decision
    time (seeded from config, optionally adjusted by conviction). The Execution
    Engine and Portfolio Manager treat them as given.
    """

    notional: Money
    take_profit: Percentage
    stop_loss: Percentage
    trailing_enabled: bool
    trailing_activation: Percentage
    trailing_distance: Percentage


# =============================================================================
# Configuration value objects (assembled from Settings at wiring time)
# =============================================================================


class SizingConfig(ValueObject):
    """Everything the Position Sizing Engine needs, as an immutable snapshot."""

    risk_per_trade_pct: float = 2.0  # % of equity risked at maximum conviction
    max_position_size_usd: float = 100.0
    min_position_size_usd: float = 0.05
    stop_loss_pct: float = 20.0  # default SL used to convert risk -> notional
    take_profit_pct: float = 40.0
    trailing_enabled: bool = True
    trailing_activation_pct: float = 25.0
    trailing_distance_pct: float = 10.0
    min_prob_roi_positive: float = 0.55
    min_confidence: float = 0.40
    min_security_score: float = 60.0


class ExposureLimits(ValueObject):
    """Concentration caps, each as a percentage of total equity."""

    max_portfolio_pct: float = 50.0
    per_token_pct: float = 20.0
    per_developer_pct: float = 25.0
    per_cluster_pct: float = 30.0
    per_strategy_pct: float = 40.0
    per_narrative_pct: float = 35.0
    per_regime_pct: float = 60.0


class DrawdownLimits(ValueObject):
    """Loss ceilings over rolling windows, plus a daily stop-loss count cap."""

    daily_pct: float = 10.0
    weekly_pct: float = 20.0
    monthly_pct: float = 35.0
    daily_max_stop_losses: int = 8
    daily_max_loss_usd: float = 200.0


class RateLimits(ValueObject):
    """Caps on how many positions/trades may be open or opened per window."""

    max_concurrent_positions: int = 5
    max_trades_per_hour: int = 20
    max_trades_per_hour_per_strategy: int = 8
    max_correlated_positions: int = 3


class KillSwitchConfig(ValueObject):
    """Thresholds that drive the graduated Kill Switch."""

    enabled: bool = True
    consecutive_losses_reduce: int = 3
    consecutive_losses_halt: int = 5
    halt_minutes: int = 60
    size_reduction_factor: float = 0.5
    dd_observation_pct: float = 10.0
    dd_paper_only_pct: float = 20.0
    dd_emergency_pct: float = 30.0


class CircuitBreakerConfig(ValueObject):
    """Thresholds that trip and clear the Circuit Breaker."""

    enabled: bool = True
    max_consecutive_errors: int = 5
    max_rpc_latency_ms: int = 3000
    cooldown_seconds: int = 300


class RiskBudget(ValueObject):
    """Per-strategy share of risk capital. Values are percentages that need not
    sum to 100 (a strategy simply may not exceed its slice)."""

    allocations: dict[str, float] = Field(default_factory=dict)

    def budget_pct(self, strategy: str) -> float:
        """The percentage budget for a strategy (100 if none configured)."""
        if not self.allocations:
            return 100.0
        return self.allocations.get(strategy.lower(), 0.0)


# =============================================================================
# Portfolio read model (what the Risk Manager needs to decide)
# =============================================================================


class OpenPositionView(ValueObject):
    """A single open position as the risk engines see it - tagged for exposure.

    The tags (``developer``, ``cluster``, ``narrative``, ``strategy``,
    ``regime``) are what the Exposure and Correlation engines aggregate over.
    They are captured at approval time and travel with the position.
    """

    token: TokenRef
    notional_usd: float
    unrealized_pnl_usd: float = 0.0
    strategy: str = "default"
    developer: str | None = None
    cluster: str | None = None
    narrative: str | None = None
    regime: str = "unknown"
    opened_at: datetime | None = None


class CapitalSnapshot(ValueObject):
    """The Capital Engine's read of the book - never commit the whole balance.

    ``available_usd`` already subtracts the mandatory liquidity reserve, so an
    engine that respects ``available_usd`` can never breach the reserve.
    """

    equity_usd: float
    cash_usd: float
    invested_usd: float
    reserve_pct: float = 35.0

    @property
    def reserve_usd(self) -> float:
        return round(self.equity_usd * self.reserve_pct / 100.0, 6)

    @property
    def available_usd(self) -> float:
        """Deployable cash after holding back the configured reserve."""
        return round(max(0.0, self.cash_usd - self.reserve_usd), 6)

    @property
    def invested_pct(self) -> float:
        if self.equity_usd <= 0:
            return 0.0
        return round(self.invested_usd / self.equity_usd * 100.0, 4)


class DrawdownSnapshot(ValueObject):
    """Rolling loss state used by the Drawdown Engine and the Kill Switch."""

    peak_equity_usd: float = 0.0
    current_equity_usd: float = 0.0
    daily_pnl_usd: float = 0.0
    weekly_pnl_usd: float = 0.0
    monthly_pnl_usd: float = 0.0
    daily_stop_losses: int = 0

    @property
    def drawdown_pct(self) -> float:
        """Peak-to-trough drawdown of equity, as a positive percentage."""
        if self.peak_equity_usd <= 0:
            return 0.0
        dd = (self.peak_equity_usd - self.current_equity_usd) / self.peak_equity_usd
        return round(max(0.0, dd) * 100.0, 4)

    def _loss_pct(self, pnl: float) -> float:
        if self.peak_equity_usd <= 0:
            return 0.0
        return round(max(0.0, -pnl) / self.peak_equity_usd * 100.0, 4)

    @property
    def daily_loss_pct(self) -> float:
        return self._loss_pct(self.daily_pnl_usd)

    @property
    def weekly_loss_pct(self) -> float:
        return self._loss_pct(self.weekly_pnl_usd)

    @property
    def monthly_loss_pct(self) -> float:
        return self._loss_pct(self.monthly_pnl_usd)


class PortfolioRiskState(ValueObject):
    """The complete portfolio picture the Risk Manager evaluates against.

    Assembled by the :class:`RiskContextBuilder` from the Portfolio Manager's
    live read model. Everything the chain needs about *current* state lives here
    so the policies stay pure (no I/O).
    """

    capital: CapitalSnapshot
    drawdown: DrawdownSnapshot = Field(default_factory=DrawdownSnapshot)
    open_positions: tuple[OpenPositionView, ...] = ()
    consecutive_losses: int = 0
    trades_last_hour: int = 0
    trades_last_hour_by_strategy: dict[str, int] = Field(default_factory=dict)
    strategy_notional_usd: dict[str, float] = Field(default_factory=dict)

    @property
    def open_count(self) -> int:
        return len(self.open_positions)


# =============================================================================
# The candidate under review
# =============================================================================


class RiskCandidate(ValueObject):
    """A single opportunity presented to the Risk Manager for a verdict.

    Distilled from the AI Committee's :class:`CommitteePrediction` (probabilities,
    confidence, regime) joined to the upstream security/wallet facts. It is the
    *what* being judged; :class:`PortfolioRiskState` is the *against what*.
    """

    token: TokenRef
    at: datetime
    # Committee output (probabilities, never a decision).
    prob_roi_positive: float = Field(ge=0.0, le=1.0)
    prob_hit_tp: float = Field(ge=0.0, le=1.0)
    prob_hit_sl: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    regime: str = "unknown"
    committee_headline: str = ""
    # Upstream facts (degrade to neutral when unknown, never to an error).
    security_approved: bool = True
    security_score: float = 50.0
    developer_score: float = 50.0
    wallet_risk_score: float = 50.0
    liquidity_score: float = 50.0
    liquidity_usd: float = 0.0
    # Strategy Engine consensus, when the roster has actually spoken for this
    # token. ``ensemble_participating == 0`` means "no opinion recorded", which
    # is different from "the strategies dislike it" and must never be read as a
    # veto — an absent opinion is not a negative one.
    ensemble_decision: str = "unknown"
    ensemble_score: float = 0.0
    ensemble_confidence: float = 0.0
    ensemble_participating: int = 0
    # Concentration tags.
    strategy: str = "default"
    developer: str | None = None
    cluster: str | None = None
    narrative: str | None = None
    correlation_id: str | None = None

    @property
    def expected_edge(self) -> float:
        """P(TP) - P(SL), in [-1, 1]. Informational only - never sizes."""
        return round(self.prob_hit_tp - self.prob_hit_sl, 4)


# =============================================================================
# Engine outputs
# =============================================================================


class SizingDecision(ValueObject):
    """The Position Sizing Engine's dynamic verdict for one candidate.

    ``sizing`` is the risk envelope the Execution Engine would act on; the extra
    fields make the number auditable - which factors scaled it and by how much.
    """

    approved: bool
    sizing: PositionSizing | None = None
    conviction: float = 0.0  # [0, 1] blended conviction that drove the size
    risk_amount_usd: float = 0.0  # capital at risk if the stop is hit
    factors: dict[str, float] = Field(default_factory=dict)
    detail: str = ""


class ExplorationGrant(ValueObject):
    """Permission to judge a candidate under *exploration* rules, at a tiny size.

    A grant is the Exploration context's answer to a single question the Risk
    Manager asks it: "the conviction gate vetoed this one — is it worth a
    fixed-size sample anyway, on the exploration budget?" It is the weakest thing
    a collaborator can hand the guardian:

    * It waives **exactly one named policy**, and only ever one from the
      conviction tuple. Safety rules are not in that tuple and there is no field
      here that could name one.
    * It carries a size the Risk Manager must use *instead of* its own sizing,
      never in addition to it. Exploration does not scale with conviction, which
      is the entire point: the size is fixed so the budget states, in advance,
      how many samples it can buy.
    * It authorises nothing. Every allocation rule still runs afterwards — the
      book's capital, exposure, correlation, drawdown and rate limits are as
      binding for an exploration trade as for any other — and the Risk Manager
      remains the only component that can approve.

    The prose fields are not decoration. A trade taken to answer a question has
    to be able to state, months later, which question and on what arithmetic;
    they travel into the assessment, the audit store and permanent memory.
    """

    notional_usd: float
    stop_loss_pct: float
    take_profit_pct: float
    #: The single conviction policy this grant waives, by name.
    waived_policy: str
    #: The cohort whose thin sample justified the trade (``None`` = the global
    #: population, for a candidate with no attribution).
    cohort_key: str | None = None
    cohort_count: int = 0
    evidence_summary: str = ""
    budget_summary: str = ""
    reasons: tuple[str, ...] = ()


class PolicyOutcome(ValueObject):
    """One risk rule's verdict on a candidate. Any failure vetoes the trade."""

    policy: str
    passed: bool
    reason: RiskRejectReason | None = None
    detail: str = ""


class RiskReason(ValueObject):
    """One legible reason behind a risk decision (driver / blocker / caveat)."""

    kind: ReasonKind
    text: str


class RiskAssessment(ValueObject):
    """The complete, self-describing verdict of one risk review.

    This is the Risk Manager's headline output and the audit record. It answers
    "approved or not, how big, and *why*" - the drivers that made it attractive,
    the blockers that could have stopped it, and the caveats to distrust it.
    Persisted verbatim so any committed trade traces back to the exact reasoning.
    """

    token: TokenRef
    at: datetime
    decision: RiskDecision
    reject_reason: RiskRejectReason | None = None
    sizing: PositionSizing | None = None
    conviction: float = 0.0
    risk_amount_usd: float = 0.0
    headline: str = ""
    reasons: tuple[RiskReason, ...] = ()
    policies: tuple[PolicyOutcome, ...] = ()
    kill_switch_level: int = 0
    circuit_breaker_open: bool = False
    correlation_id: str | None = None
    #: Set when this decision was taken under the exploration programme. Kept on
    #: the assessment (and therefore in the audit store) because a trade sized
    #: for evidence rather than for edge must never be readable, afterwards, as
    #: an ordinary conviction trade that happened to be small.
    exploration: ExplorationGrant | None = None

    @property
    def is_exploration(self) -> bool:
        return self.exploration is not None

    @property
    def approved(self) -> bool:
        return self.decision is RiskDecision.APPROVE

    @property
    def drivers(self) -> tuple[str, ...]:
        return tuple(r.text for r in self.reasons if r.kind is ReasonKind.DRIVER)

    @property
    def blockers(self) -> tuple[str, ...]:
        return tuple(r.text for r in self.reasons if r.kind is ReasonKind.BLOCKER)

    @property
    def caveats(self) -> tuple[str, ...]:
        return tuple(r.text for r in self.reasons if r.kind is ReasonKind.CAVEAT)


# =============================================================================
# Defence-layer state
# =============================================================================


class KillSwitchState(ValueObject):
    """The current Kill Switch posture - which level and why."""

    level: KillSwitchLevel = KillSwitchLevel.NONE
    reason: str = ""
    consecutive_losses: int = 0
    size_factor: float = 1.0  # multiplier the sizing engine applies
    since: datetime | None = None
    halted_until: datetime | None = None

    @property
    def engaged(self) -> bool:
        return self.level is not KillSwitchLevel.NONE

    @property
    def blocks_entries(self) -> bool:
        return self.level.blocks_entries


class CircuitBreakerState(ValueObject):
    """The Circuit Breaker posture - open (no trading) or closed (normal)."""

    open: bool = False
    reasons: tuple[CircuitBreakerReason, ...] = ()
    detail: str = ""
    since: datetime | None = None
    open_until: datetime | None = None


class ExposureBreakdown(ValueObject):
    """Concentration along one axis - each key's share of equity, in percent."""

    axis: ExposureAxis
    by_key: dict[str, float] = Field(default_factory=dict)

    def pct_for(self, key: str | None) -> float:
        if key is None:
            return 0.0
        return self.by_key.get(key, 0.0)


# =============================================================================
# Persistable snapshots
# =============================================================================


class RiskSnapshot(ValueObject):
    """A dashboard-facing snapshot of the whole risk subsystem at a moment."""

    at: datetime
    kill_switch: KillSwitchState
    circuit_breaker: CircuitBreakerState
    emergency_mode: bool = False
    open_positions: int = 0
    portfolio_exposure_pct: float = 0.0
    daily_loss_pct: float = 0.0
    drawdown_pct: float = 0.0
    consecutive_losses: int = 0
    approvals_session: int = 0
    rejections_session: int = 0
    #: How many of ``approvals_session`` were exploration samples. Reported
    #: separately so a dashboard never presents "12 approvals" without the fact
    #: that eleven of them were dollar-sized evidence purchases.
    exploration_approvals_session: int = 0


class RiskConfig(ValueObject):
    """The full, immutable risk configuration - one bundle assembled from
    Settings and handed to the factory that wires the Risk Manager."""

    sizing: SizingConfig = Field(default_factory=SizingConfig)
    exposure: ExposureLimits = Field(default_factory=ExposureLimits)
    drawdown: DrawdownLimits = Field(default_factory=DrawdownLimits)
    rates: RateLimits = Field(default_factory=RateLimits)
    kill_switch: KillSwitchConfig = Field(default_factory=KillSwitchConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    budget: RiskBudget = Field(default_factory=RiskBudget)
    min_developer_score: float = 40.0
    max_wallet_risk: float = 70.0
    min_liquidity_usd: float = 5_000.0
    #: Whether the Strategy Engine's ensemble may veto an entry. Off by default:
    #: turning it on is a deliberate change of posture, and the flag existed for
    #: a long time reading nothing at all.
    gate_on_ensemble: bool = False
    #: Minimum ensemble conviction, in [-1, 1], for a BUY consensus to stand.
    min_ensemble_score: float = 0.0


class RiskControlState(ValueObject):
    """The durable defence-layer state - survives process restarts.

    The Kill Switch level, the Circuit Breaker and Emergency Mode must not reset
    to "all clear" just because a container restarted; they are persisted and
    reloaded so a halt stays a halt until a human (or recovery) clears it.
    """

    kill_switch: KillSwitchState = Field(default_factory=KillSwitchState)
    circuit_breaker: CircuitBreakerState = Field(default_factory=CircuitBreakerState)
    emergency_mode: bool = False


def to_money(amount_usd: float) -> Money:
    """Wrap a USD float in a :class:`Money` (Decimal-backed, no float error)."""
    return Money(amount=Decimal(str(round(amount_usd, 6))))
