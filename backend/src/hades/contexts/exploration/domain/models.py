"""The vocabulary of Exploration — budgets, evidence, and a verdict.

Nothing in this module can act. There is no order here, no position, no balance
and no notion of a trading mode; the strongest thing it can say is "this
candidate is *eligible* to be considered under exploration rules, for at most
this many dollars". Turning that into a trade is the Risk Manager's job and only
its job, and the type system is arranged so that no other reading is available:
:class:`ExplorationVerdict` has no approval on it, only an eligibility flag and
a ceiling.

Two value objects carry the design.

:class:`EvidenceStatus` answers *may exploration run at all?* — and answers it
with arithmetic a person can check: how many settled lessons exist, how many of
each class, and what the configured targets are. The both-classes condition is
not a nicety. A single-class dataset has an undefined AUC, so no validation gate
can pass on it however many samples it holds; a platform with 900 losses and no
wins is exactly as unable to validate a model as one with no trades at all, and
:attr:`EvidenceStatus.sufficient` says so.

:class:`ExplorationBudget` is the ceiling, expressed in four independent
dimensions (per trade, per day, per week, per lifetime). They are independent on
purpose: a daily cap alone permits an unbounded total spend given enough days,
and a lifetime cap alone permits the whole budget to burn in one bad afternoon.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, ValidationInfo, field_validator

from hades.shared_kernel.domain.base import ValueObject


class ExplorationDecline(StrEnum):
    """Why a candidate was not granted an exploration trade.

    One member per condition, so a decline is always traceable to the rule that
    produced it and the metrics can be sliced by cause. The distinction between
    the three budget members matters operationally: "the day's allowance is
    spent" resolves itself overnight, "the programme's total is spent" never
    does, and an operator reading a single ``budget_exhausted`` counter could not
    tell those apart.
    """

    #: The operator has not switched exploration on.
    DISABLED = "disabled"
    #: The memory already holds what exploration exists to buy. Terminal.
    EVIDENCE_SUFFICIENT = "evidence_sufficient"
    #: The candidate is not merely unproven, it is unattractive: its probability
    #: sits below the exploration floor, or above the ceiling (in which case the
    #: production path would have taken it and this is not exploration at all).
    OUTSIDE_BAND = "outside_band"
    #: Every cohort this candidate belongs to is already sampled to target. One
    #: more trade here buys precision the platform does not currently lack.
    COHORT_SATURATED = "cohort_saturated"
    DAILY_BUDGET = "daily_budget"
    WEEKLY_BUDGET = "weekly_budget"
    TOTAL_BUDGET = "total_budget"
    DAILY_TRADES = "daily_trades"
    WEEKLY_TRADES = "weekly_trades"
    #: The ledger could not be read. Exploration is optional spending, so an
    #: unreadable budget means *no* trade — the fail-closed direction.
    UNAVAILABLE = "unavailable"


class EvidenceTarget(ValueObject):
    """How much evidence is "enough", stated as a condition, not a feeling.

    ``min_lessons`` is the sample the committee's validation gate needs to mean
    anything; ``min_per_class`` is the guard against reaching that sample with
    every trade on one side of zero, which would leave the AUC undefined and the
    gate unpassable. ``cohort_target`` drives *selection* rather than shutdown:
    it is the number of settled lessons at which a cohort (a developer, a
    launchpad, a narrative) stops being interesting to sample.
    """

    min_lessons: int = Field(default=60, ge=1)
    min_per_class: int = Field(default=15, ge=1)
    cohort_target: int = Field(default=8, ge=1)


class EvidenceStatus(ValueObject):
    """What the memory currently holds, and whether that is enough.

    Assembled from settled lessons only. Simulations are deliberately excluded:
    the entire premise of exploration is that the platform lacks *ground truth*,
    and a backtest — however voluminous — is a statement about a model, not about
    the market. Counting simulations here would let the lab talk the platform out
    of gathering the very evidence it cannot produce.
    """

    lessons: int = 0
    positive: int = 0
    negative: int = 0
    #: Settled-lesson counts per cohort key, formatted ``"<dimension>=<value>"``.
    cohorts: dict[str, int] = Field(default_factory=dict)
    target: EvidenceTarget = Field(default_factory=EvidenceTarget)
    #: False when the memory could not be read. Distinguished from an empty
    #: memory on purpose: a young platform and a broken one looked identical for
    #: weeks once, and that was the most expensive thing the last audit found.
    available: bool = True

    @property
    def sufficient(self) -> bool:
        """Whether exploration has bought what it exists to buy.

        All three conditions, and the two class conditions are not redundant with
        the total: 60 lessons that are all losses satisfy ``min_lessons`` and
        still cannot validate a model.
        """
        if not self.available:
            return False
        return (
            self.lessons >= self.target.min_lessons
            and self.positive >= self.target.min_per_class
            and self.negative >= self.target.min_per_class
        )

    @property
    def summary(self) -> str:
        """The arithmetic, in one line, for the explanation of every verdict."""
        if not self.available:
            return "memory unreadable — evidence unknown"
        return (
            f"{self.lessons}/{self.target.min_lessons} lessons, "
            f"{self.positive}+/{self.negative}- "
            f"(need {self.target.min_per_class} of each)"
        )

    def cohort_count(self, key: str) -> int:
        return self.cohorts.get(key, 0)


class ExplorationBudget(ValueObject):
    """The four independent ceilings on exploration spending.

    ``per_trade_usd`` is a *fixed* size, not a maximum the sizing engine may
    approach: exploration deliberately does not scale with conviction, because a
    size that grows with belief reintroduces the very belief the programme exists
    to test. Every trade costs the same, so the budget also states, exactly, how
    many trades it can ever buy.
    """

    per_trade_usd: float = Field(default=1.0, gt=0.0)
    daily_usd: float = Field(default=10.0, gt=0.0)
    weekly_usd: float = Field(default=40.0, gt=0.0)
    total_usd: float = Field(default=250.0, gt=0.0)
    max_trades_per_day: int = Field(default=10, ge=1)
    max_trades_per_week: int = Field(default=40, ge=1)

    @property
    def max_trades_ever(self) -> int:
        """How many exploration trades the lifetime budget can fund at all."""
        return int(self.total_usd // self.per_trade_usd)


class ExplorationSpend(ValueObject):
    """What has actually been spent, read back from the append-only ledger.

    Derived rather than accumulated in memory: the ledger is the truth, and a
    counter held in a process would reset on restart and silently re-authorise a
    day's budget every deploy.
    """

    day_usd: float = 0.0
    day_trades: int = 0
    week_usd: float = 0.0
    week_trades: int = 0
    total_usd: float = 0.0
    total_trades: int = 0

    def remaining(self, budget: ExplorationBudget) -> dict[str, float]:
        return {
            "day_usd": round(max(0.0, budget.daily_usd - self.day_usd), 6),
            "week_usd": round(max(0.0, budget.weekly_usd - self.week_usd), 6),
            "total_usd": round(max(0.0, budget.total_usd - self.total_usd), 6),
            "day_trades": max(0, budget.max_trades_per_day - self.day_trades),
            "week_trades": max(0, budget.max_trades_per_week - self.week_trades),
        }


class ExplorationConfig(ValueObject):
    """The complete, immutable exploration policy, assembled from settings.

    ``min_prob_roi_positive`` / ``max_prob_roi_positive`` bound the band this
    programme samples from. The floor exists because exploration buys *evidence*,
    not lottery tickets: a candidate the committee puts at 0.05 is not an open
    question. The ceiling exists because a candidate above it would have cleared
    the production gates on its own — taking it here would spend the exploration
    budget on a trade the platform was going to make anyway, and would flatter
    the programme's own results.
    """

    enabled: bool = False
    budget: ExplorationBudget = Field(default_factory=ExplorationBudget)
    target: EvidenceTarget = Field(default_factory=EvidenceTarget)
    min_prob_roi_positive: float = Field(default=0.35, ge=0.0, le=1.0)
    max_prob_roi_positive: float = Field(default=0.55, ge=0.0, le=1.0)
    min_confidence: float = Field(default=0.15, ge=0.0, le=1.0)
    #: Risk envelope for an exploration trade. Tighter than production by
    #: default: the point is to learn what happens, and a wide stop turns a
    #: cheap question into an expensive one.
    stop_loss_pct: float = Field(default=12.0, gt=0.0)
    take_profit_pct: float = Field(default=30.0, gt=0.0)
    #: Dimensions used to decide whether a candidate is under-sampled. Order is
    #: irrelevant; membership is the whole rule.
    cohort_dimensions: tuple[str, ...] = ("developer", "launchpad", "narrative", "cluster")

    @field_validator("max_prob_roi_positive")
    @classmethod
    def _band_ordered(cls, value: float, info: ValidationInfo) -> float:
        # An inverted band silently accepts nothing, and "exploration is on but
        # never fires" is the hardest failure to notice on this whole platform.
        floor = info.data.get("min_prob_roi_positive")
        if isinstance(floor, float) and value < floor:
            raise ValueError(
                "max_prob_roi_positive must not be below min_prob_roi_positive "
                f"({value} < {floor}): an inverted band accepts no candidate at all"
            )
        return value


class ExplorationCandidate(ValueObject):
    """What the policy needs to know about a candidate — and nothing else.

    Deliberately not the Risk Manager's ``RiskCandidate``. Exploration has no
    business knowing a candidate's sizing, its portfolio context or its security
    verdict: those are inputs to rules that exploration does not get to waive,
    and a type that carried them would invite a rule here that quietly did.
    """

    subject: str
    prob_roi_positive: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    #: Cohort keys as the committee established them (developer, launchpad,
    #: narrative, cluster). Absent keys are simply unknown, never guessed.
    cohorts: dict[str, str] = Field(default_factory=dict)
    correlation_id: str | None = None

    @field_validator("subject")
    @classmethod
    def _subject_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("subject: must not be blank")
        return value


class ExplorationVerdict(ValueObject):
    """The answer: eligible or not, at what size, and why — always why.

    ``granted`` is *eligibility*, not authorisation. The Risk Manager may still
    reject a granted candidate on any of the rules exploration does not touch,
    and frequently will.
    """

    granted: bool
    subject: str
    notional_usd: float = 0.0
    stop_loss_pct: float = 0.0
    take_profit_pct: float = 0.0
    decline: ExplorationDecline | None = None
    #: The cohort that justified the trade, if one did.
    cohort_key: str | None = None
    cohort_count: int = 0
    reasons: tuple[str, ...] = ()
    evidence_summary: str = ""
    budget_summary: str = ""
    at: datetime | None = None

    @property
    def headline(self) -> str:
        if self.granted:
            return (
                f"Exploration grant ${self.notional_usd:.4f} "
                f"(cohort {self.cohort_key or 'global'} at {self.cohort_count} lessons)"
            )
        return f"Exploration declined: {(self.decline or ExplorationDecline.DISABLED).value}"


class ExplorationRecord(ValueObject):
    """One granted-and-committed exploration trade, as written to the ledger.

    The ledger is append-only and is the *only* accounting of the programme:
    spend is always derived from it, never from a counter in a process. A row is
    written when the Risk Manager approves, not when the grant is issued, so a
    candidate that clears exploration and is then vetoed by an allocation rule
    costs the budget nothing.
    """

    subject: str
    granted_at: datetime
    notional_usd: float
    cohort_key: str | None = None
    reason: str = ""
    correlation_id: str | None = None


class ExplorationProgress(ValueObject):
    """How far along the programme is, and whether it can finish at all.

    The three sufficiency conditions are a conjunction, so progress is the
    **binding** one — the minimum — never the average. Averaging would report a
    programme at 70% when it holds 60 lessons that are all losses, which is the
    single state the whole evidence design exists to prevent: it satisfies the
    count, cannot validate a model, and would look nearly finished.

    :attr:`budget_exhausts_first` is the question nobody was asking. Exploration
    buys lessons with a fixed-size budget, so there is a knowable answer to "can
    the remaining runway still buy the evidence we lack?" — and if it cannot, the
    programme is already doomed and the operator should learn that now rather
    than when the last dollar goes. It is deliberately conservative: unknown
    conversion means no alarm, because crying wolf on a young programme with two
    trades of history would train the operator to ignore it.
    """

    lessons_needed: int = 0
    positive_needed: int = 0
    negative_needed: int = 0
    #: 0.0 to 1.0 against the binding condition. 1.0 means sufficiency is reached.
    pct_complete: float = 0.0
    #: Exploration trades the lifetime budget can still fund.
    trades_remaining: int = 0
    #: Observed settled lessons per exploration trade. ``None`` until there is
    #: enough history to divide by — never defaulted to 1.0, which would assume
    #: the conversion this figure exists to measure.
    lessons_per_trade: float | None = None
    #: Observed accumulation rate and the resulting estimate. Both ``None`` when
    #: the history is too short to say anything honest.
    lessons_per_day: float | None = None
    eta_days: float | None = None
    #: True only when the arithmetic is known *and* says the runway is short.
    budget_exhausts_first: bool = False
    note: str = ""

    @property
    def complete(self) -> bool:
        return self.pct_complete >= 1.0

    @property
    def stalled(self) -> bool:
        """Spending with a measured rate of zero — the failure mode that matters.

        A programme burning budget while the lesson count stays flat is the state
        the metrics docstring calls out as the one worth catching within an hour.
        """
        return self.lessons_per_day is not None and self.lessons_per_day <= 0.0


class ExplorationStatus(ValueObject):
    """The whole programme in one object, for the API and the dashboard."""

    enabled: bool
    active: bool
    evidence: EvidenceStatus
    budget: ExplorationBudget
    spend: ExplorationSpend
    #: Why it is not currently running, when it is not.
    inactive_reason: str = ""
    granted_total: int = 0
    declined_total: int = 0
    declines_by_reason: dict[str, int] = Field(default_factory=dict)
    progress: ExplorationProgress = Field(default_factory=ExplorationProgress)


__all__ = [
    "EvidenceStatus",
    "EvidenceTarget",
    "ExplorationBudget",
    "ExplorationCandidate",
    "ExplorationConfig",
    "ExplorationDecline",
    "ExplorationRecord",
    "ExplorationSpend",
    "ExplorationStatus",
    "ExplorationVerdict",
]
