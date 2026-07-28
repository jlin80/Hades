"""Value objects and aggregates of the Research Lab.

Everything the lab produces is an immutable, auditable fact: an experiment
specification, a backtest result, a walk-forward window, a Monte-Carlo robustness
report, an optimisation outcome, a shadow strategy's virtual ledger, a candidate
strategy genome, a performance report, a comparison, a promotion decision.

The lab works exclusively on *copies* of historical data and produces
*knowledge*. Nothing in here can place an order, size a position, or enable live
trading — there are, by construction, no execution types imported anywhere in
this context. A ``CandidateStrategy`` is a rule genome, never runnable code, and a
``PromotionDecision`` is a *recommendation* that still needs a human hand.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field

from hades.shared_kernel.domain.base import ValueObject
from hades.shared_kernel.domain.identifiers import EntityId, new_id


def _now() -> datetime:
    return datetime.now(UTC)


# =============================================================================
# Enumerations
# =============================================================================


class ExperimentKind(StrEnum):
    """What an experiment measures — every production change becomes one of these."""

    BACKTEST = "backtest"
    WALK_FORWARD = "walk_forward"
    MONTE_CARLO = "monte_carlo"
    OPTIMIZATION = "optimization"
    SHADOW = "shadow"
    FEATURE_DISCOVERY = "feature_discovery"
    MODEL_COMPARISON = "model_comparison"
    STRATEGY_COMPARISON = "strategy_comparison"
    PARAMETER_CHANGE = "parameter_change"


class ExperimentStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"


class CandidateKind(StrEnum):
    """The four things the lab can propose for promotion."""

    FEATURE = "feature"
    SIGNAL = "signal"
    STRATEGY = "strategy"
    MODEL = "model"


class StrategyArchetype(StrEnum):
    """The family a candidate strategy belongs to."""

    MOMENTUM = "momentum"
    MEAN_REVERSION = "mean_reversion"
    LIQUIDITY_BREAKOUT = "liquidity_breakout"
    SMART_MONEY_FOLLOW = "smart_money_follow"
    WALLET_ROTATION = "wallet_rotation"
    LAUNCH_DETECTION = "launch_detection"
    NEWS_DRIVEN = "news_driven"
    SOCIAL_MOMENTUM = "social_momentum"
    ORDER_FLOW = "order_flow"
    MICROSTRUCTURE = "microstructure"


class ValidationStage(StrEnum):
    """The one-way pipeline every strategy must climb — never skip a stage."""

    TRAINING = "training"
    VALIDATION = "validation"
    FORWARD_TEST = "forward_test"
    PAPER_REPLAY = "paper_replay"
    SHADOW = "shadow"
    PRODUCTION = "production"


class PromotionOutcome(StrEnum):
    APPROVED = "approved"  # criteria met — still needs a human to pull the lever
    REJECTED = "rejected"


# =============================================================================
# Performance metrics — the shared scorecard
# =============================================================================


class PerformanceMetrics(ValueObject):
    """The full scorecard for a sequence of (virtual) trades.

    Never optimise a single number: ROI alone is a trap. Every comparison in the
    lab reads the whole vector — drawdown, Sharpe, Sortino, profit factor,
    expectancy and consistency included.
    """

    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_return: float = 0.0  # sum of per-trade returns (R or fraction)
    avg_return: float = 0.0
    expectancy: float = 0.0  # avg return per trade
    profit_factor: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    max_drawdown: float = 0.0  # positive fraction, e.g. 0.32 = 32%
    calmar: float = 0.0
    recovery_factor: float = 0.0
    consistency: float = 0.0  # fraction of positive rolling windows (0..1)
    avg_holding: float = 0.0  # average holding time in seconds
    gross_profit: float = 0.0
    gross_loss: float = 0.0

    @property
    def is_empty(self) -> bool:
        return self.trades == 0


class TradeRecord(ValueObject):
    """A single (virtual) trade the lab scores — never a real fill."""

    opened_at: datetime
    closed_at: datetime
    ret: float  # realised return (fraction of risk or of capital)
    holding_seconds: float = 0.0
    mint: str | None = None
    reason: str = ""


# =============================================================================
# Experiments
# =============================================================================


class ExperimentSpec(ValueObject):
    """The declarative recipe for an experiment. Nothing runs without one."""

    experiment_id: EntityId = Field(default_factory=new_id)
    name: str
    kind: ExperimentKind
    hypothesis: str = ""
    params: dict[str, float | int | str | bool] = Field(default_factory=dict)
    dataset_id: str | None = None
    from_iso: str | None = None
    to_iso: str | None = None
    created_at: datetime = Field(default_factory=_now)


class ExperimentResult(ValueObject):
    """The reproducible outcome of a finished experiment."""

    experiment_id: EntityId
    name: str
    kind: ExperimentKind
    status: ExperimentStatus
    metrics: dict[str, float] = Field(default_factory=dict)
    conclusion: str = ""
    hypothesis: str = ""
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime = Field(default_factory=_now)
    error: str | None = None


# =============================================================================
# Backtesting / Walk-forward / Monte-Carlo
# =============================================================================


class MarketConditions(ValueObject):
    """The frictions a professional backtest must reproduce — not just OHLC.

    Slippage, fees, latency, liquidity depth and volatility all bleed edge; a
    backtest that ignores them lies. Defaults are conservative meme-coin values.
    """

    slippage_bps: float = 100.0
    fee_bps: float = 25.0
    latency_ms: float = 400.0
    liquidity_usd: float = 25_000.0
    volatility: float = 0.0


class BacktestResult(ValueObject):
    """The result of replaying a strategy over a historical window with frictions."""

    backtest_id: EntityId = Field(default_factory=new_id)
    strategy: str
    from_iso: str | None = None
    to_iso: str | None = None
    conditions: MarketConditions = Field(default_factory=MarketConditions)
    metrics: PerformanceMetrics = Field(default_factory=PerformanceMetrics)
    equity_curve: tuple[float, ...] = ()
    finished_at: datetime = Field(default_factory=_now)


class WalkForwardWindow(ValueObject):
    """One train→test window of a walk-forward run."""

    index: int
    train_from: str
    train_to: str
    test_from: str
    test_to: str
    in_sample: PerformanceMetrics
    out_of_sample: PerformanceMetrics


class WalkForwardResult(ValueObject):
    """Rolling out-of-sample validation — the real test of a strategy's edge."""

    strategy: str
    windows: tuple[WalkForwardWindow, ...] = ()
    aggregate_oos: PerformanceMetrics = Field(default_factory=PerformanceMetrics)
    efficiency: float = 0.0  # OOS/IS return ratio (robustness; 1.0 = no decay)
    finished_at: datetime = Field(default_factory=_now)


class MonteCarloResult(ValueObject):
    """Robustness under thousands of perturbed scenarios.

    Perturbs slippage, latency, liquidity, ordering and drops trades, then reports
    the distribution of outcomes so a strategy that only worked on one lucky path
    is exposed.
    """

    strategy: str
    simulations: int = 0
    mean_return: float = 0.0
    median_return: float = 0.0
    std_return: float = 0.0
    p05_return: float = 0.0
    p95_return: float = 0.0
    worst_drawdown: float = 0.0
    prob_profit: float = 0.0  # fraction of paths ending positive
    robustness: float = 0.0  # 0..1 composite robustness score
    finished_at: datetime = Field(default_factory=_now)


class OptimizationResult(ValueObject):
    """The best parameter set found under a *multi-objective* score.

    ROI is never optimised alone; the objective blends return, drawdown, Sharpe,
    Sortino, profit factor and expectancy so the winner is robust, not curve-fit.
    """

    strategy: str
    objective: str
    best_params: dict[str, float] = Field(default_factory=dict)
    best_score: float = 0.0
    best_metrics: PerformanceMetrics = Field(default_factory=PerformanceMetrics)
    evaluations: int = 0
    trials: tuple[dict[str, float], ...] = ()
    finished_at: datetime = Field(default_factory=_now)


# =============================================================================
# Strategies (candidate + shadow)
# =============================================================================


class StrategyGenome(ValueObject):
    """A rule-based strategy as data, never code.

    A genome is a set of named thresholds over the feature vector plus the
    archetype that interprets them. It is executed by a pure evaluator in the lab;
    it can never be turned into a live order — promotion is a separate human step.
    """

    archetype: StrategyArchetype
    thresholds: dict[str, float] = Field(default_factory=dict)
    weights: dict[str, float] = Field(default_factory=dict)
    entry_score: float = 0.6  # min blended score to take a virtual trade
    take_profit: float = 0.30
    stop_loss: float = 0.15
    max_holding_seconds: float = 3600.0


class CandidateStrategy(ValueObject):
    """A named, versioned candidate the lab is developing and scoring."""

    candidate_id: EntityId = Field(default_factory=new_id)
    name: str
    archetype: StrategyArchetype
    genome: StrategyGenome
    version: int = 1
    stage: ValidationStage = ValidationStage.TRAINING
    metrics: PerformanceMetrics = Field(default_factory=PerformanceMetrics)
    created_at: datetime = Field(default_factory=_now)


class VirtualTrade(ValueObject):
    """A shadow strategy's paper fill — capital-free, order-free, portfolio-free."""

    mint: str
    opened_at: datetime
    entry_score: float
    size_usd: float = 0.0
    closed_at: datetime | None = None
    ret: float | None = None
    reason: str = ""


class ShadowStrategy(ValueObject):
    """A strategy running live-shaped but fully virtual.

    It sees exactly what a production strategy sees and behaves identically — the
    *only* difference is that every trade is virtual: no order is sent, no capital
    committed, the Portfolio is never touched.
    """

    shadow_id: EntityId = Field(default_factory=new_id)
    name: str
    archetype: StrategyArchetype
    genome: StrategyGenome
    open_trades: tuple[VirtualTrade, ...] = ()
    metrics: PerformanceMetrics = Field(default_factory=PerformanceMetrics)
    updated_at: datetime = Field(default_factory=_now)


# =============================================================================
# Comparisons
# =============================================================================


class ComparisonEntry(ValueObject):
    """One competitor's line in a comparison table."""

    label: str
    metrics: PerformanceMetrics


class StrategyComparison(ValueObject):
    """A ranked comparison across strategies on the full scorecard."""

    entries: tuple[ComparisonEntry, ...] = ()
    ranking: tuple[str, ...] = ()  # labels best→worst
    objective: str = "expectancy"
    created_at: datetime = Field(default_factory=_now)


class ModelComparison(ValueObject):
    """Production vs candidate vs shadow model on classification + trading metrics."""

    production: dict[str, float] = Field(default_factory=dict)
    candidate: dict[str, float] = Field(default_factory=dict)
    shadow: dict[str, float] = Field(default_factory=dict)
    deltas: dict[str, float] = Field(default_factory=dict)  # candidate minus production
    verdict: str = ""
    created_at: datetime = Field(default_factory=_now)


# =============================================================================
# Feature discovery
# =============================================================================


class FeatureProposal(ValueObject):
    """A proposed feature change — the lab only ever *proposes*, never removes."""

    proposal_id: EntityId = Field(default_factory=new_id)
    feature: str
    action: str  # "add" | "flag_redundant" | "flag_irrelevant" | "combine"
    importance: float = 0.0
    correlation: float = 0.0  # with an existing feature, if redundant
    rationale: str = ""
    combines: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=_now)


# =============================================================================
# Promotion (fail-closed, human-gated)
# =============================================================================


class PromotionCriteria(ValueObject):
    """Configurable bar a candidate must clear to be *recommended* for promotion.

    Clearing every threshold yields APPROVED — a recommendation only. Live
    promotion always requires an explicit human action outside the lab.
    """

    min_trades: int = 500
    min_sharpe: float = 1.0
    max_drawdown: float = 0.25
    min_profit_factor: float = 1.3
    min_expectancy: float = 0.0
    require_paper_positive: bool = True
    require_shadow_positive: bool = True
    require_manual_approval: bool = True


class PromotionDecision(ValueObject):
    """The outcome of evaluating a candidate against its criteria."""

    candidate_id: EntityId
    candidate_name: str
    kind: CandidateKind
    outcome: PromotionOutcome
    stage: ValidationStage
    passed: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    metrics: PerformanceMetrics = Field(default_factory=PerformanceMetrics)
    rationale: str = ""
    manual_approved: bool = False  # never true without a human action
    decided_at: datetime = Field(default_factory=_now)

    @property
    def promotable(self) -> bool:
        """A candidate is promotable only when it cleared the bar AND a human
        approved it. The lab can never satisfy the second half by itself."""
        return (
            self.outcome is PromotionOutcome.APPROVED and self.manual_approved and not self.failed
        )


# =============================================================================
# Knowledge
# =============================================================================


class Hypothesis(ValueObject):
    """A recorded research hypothesis and how it resolved."""

    hypothesis_id: EntityId = Field(default_factory=new_id)
    statement: str
    rationale: str = ""
    status: str = "open"  # open | confirmed | refuted
    evidence: dict[str, float] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)


class KnowledgeEntry(ValueObject):
    """An append-only note in the Knowledge Base — history is never deleted."""

    entry_id: EntityId = Field(default_factory=new_id)
    category: str  # experiment | result | error | conclusion | dataset | comparison
    title: str
    body: str = ""
    data: dict[str, float | int | str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)


class ResearchReport(ValueObject):
    """A generated periodic report, stored for future consultation."""

    report_id: EntityId = Field(default_factory=new_id)
    period: str  # daily | weekly | monthly
    title: str
    summary: str = ""
    sections: dict[str, str] = Field(default_factory=dict)
    stats: dict[str, float] = Field(default_factory=dict)
    generated_at: datetime = Field(default_factory=_now)


# =============================================================================
# Small numeric helpers reused across engines (kept here so the domain owns the
# definition of every metric it exposes).
# =============================================================================


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    return numerator / denominator if denominator else default


def clamp01(value: float) -> float:
    if math.isnan(value):
        return 0.0
    return max(0.0, min(1.0, value))
