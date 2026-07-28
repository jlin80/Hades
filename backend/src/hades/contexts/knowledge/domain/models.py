"""Knowledge value objects — what the platform remembers, and how sure it is.

The vocabulary here is deliberately the context's own. Nothing in this module
mentions an order, a position, a portfolio or a trading mode, and nothing
imports another context: the whole point of Knowledge is to be safe to plug into
every producer on the platform, and a memory that shares a type with the
execution path is one refactor away from being a channel into it.

Three ideas carry the design:

* **Provenance** (:class:`KnowledgeSource`) — where a fact came from.
* **Verification** (:class:`Verification`) — how strongly it is known. A backtest
  and a settled paper trade are both knowledge; only one of them is ground truth,
  and the type system should not let a caller confuse them.
* **The decision/outcome join** (:class:`Decision` → :class:`Lesson`) — the piece
  the platform never had. A decision freezes the evidence *as it stood when the
  decision was taken*; the outcome arrives much later; a lesson is the two of
  them married. Freezing at decision time is what keeps the training signal free
  of the future.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator

from hades.shared_kernel.domain.base import ValueObject
from hades.shared_kernel.domain.identifiers import EntityId, new_id


class KnowledgeSource(StrEnum):
    """Which producer a record came from.

    The membership of this enum is the audit-visible answer to "what feeds the
    memory". Adding a producer means adding a member here, which means the
    translation table in the composition root has to name it — a new source
    cannot enter the store anonymously.
    """

    SCANNER = "scanner"
    SECURITY = "security"
    WALLET_INTELLIGENCE = "wallet_intelligence"
    COMMITTEE = "committee"
    PAPER_TRADING = "paper_trading"
    SHADOW_TRADING = "shadow_trading"
    RESEARCH_LAB = "research_lab"
    BACKTEST = "backtest"
    WALK_FORWARD = "walk_forward"
    MONTE_CARLO = "monte_carlo"
    EXECUTED_TRADE = "executed_trade"


class KnowledgeKind(StrEnum):
    """What sort of statement a record makes."""

    #: A measurement of the world ("this pool held $12k at 14:03").
    OBSERVATION = "observation"
    #: A judgement derived from measurements ("this contract is unsafe").
    ASSESSMENT = "assessment"
    #: A statement about the future ("P(ROI+) = 0.61").
    PREDICTION = "prediction"
    #: What actually happened ("closed at -18%, hit the stop").
    OUTCOME = "outcome"
    #: What would have happened under a model (backtest, Monte Carlo, shadow).
    SIMULATION = "simulation"
    #: A research finding about the platform itself, not about a token.
    EXPERIMENT = "experiment"


class Verification(StrEnum):
    """How strongly a record is known to be true.

    Ordering matters and is exposed through :data:`VERIFICATION_RANK`, so a
    consumer can demand a floor ("ground truth only") rather than enumerating
    the levels it happens to know about today.
    """

    #: Settled reality: a trade that opened, closed, and paid out. Paper trades
    #: count — the fills are simulated, but the price path was the real market.
    REALISED = "realised"
    #: Produced by a model over real history: backtests, walk-forward, shadow.
    #: True about the model, not about the world.
    SIMULATED = "simulated"
    #: Asserted by a third party (a DEX API, a metadata provider). Believed, not
    #: checked.
    REPORTED = "reported"
    #: Provenance known, truth not established.
    UNVERIFIED = "unverified"


#: Higher is stronger. Used by queries that need a verification floor.
VERIFICATION_RANK: dict[Verification, int] = {
    Verification.UNVERIFIED: 0,
    Verification.REPORTED: 1,
    Verification.SIMULATED: 2,
    Verification.REALISED: 3,
}


class SubjectType(StrEnum):
    """What a record is *about*, so the store can be indexed sensibly."""

    TOKEN = "token"
    WALLET = "wallet"
    STRATEGY = "strategy"
    MODEL = "model"
    PLATFORM = "platform"


def _finite(value: float, field: str) -> float:
    """Reject NaN/inf at the boundary rather than storing poison."""
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"{field}: must be a finite number, got {value!r}")
    return value


class KnowledgeEnvelope(ValueObject):
    """The **only** shape the outside world may hand to Knowledge.

    This is the anti-corruption layer's currency. A producer's domain event is
    translated into one of these by the composition root; Knowledge itself never
    sees — and never imports — the originating type. Keeping the inbound surface
    to a single flat value object is what makes the isolation test meaningful:
    there is nothing to import, so there is no import to forbid.
    """

    source: KnowledgeSource
    kind: KnowledgeKind
    verification: Verification
    subject: str
    subject_type: SubjectType = SubjectType.TOKEN
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any] = Field(default_factory=dict)
    #: Named, already-normalised numeric features, when the producer has them.
    features: dict[str, float] = Field(default_factory=dict)
    #: Ties a record to the decision it belongs to, so a prediction and its
    #: outcome can be reunited across contexts and across hours.
    correlation_id: str | None = None

    @field_validator("subject")
    @classmethod
    def _subject_not_blank(cls, value: str) -> str:
        # A record about nothing is unqueryable and therefore worse than absent:
        # it inflates every count while answering no question.
        if not value.strip():
            raise ValueError("subject: must not be blank")
        return value

    @field_validator("features")
    @classmethod
    def _features_finite(cls, value: dict[str, float]) -> dict[str, float]:
        return {name: _finite(float(v), f"features[{name}]") for name, v in value.items()}


class Observation(ValueObject):
    """One immutable thing the platform knows, as stored.

    An envelope that has been accepted. The extra fields over
    :class:`KnowledgeEnvelope` are the store's own: an identity and the moment of
    recording (which is not the moment of occurrence — a backtest run today can
    record an observation about last month).
    """

    observation_id: EntityId = Field(default_factory=new_id)
    source: KnowledgeSource
    kind: KnowledgeKind
    verification: Verification
    subject: str
    subject_type: SubjectType
    occurred_at: datetime
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any] = Field(default_factory=dict)
    features: dict[str, float] = Field(default_factory=dict)
    correlation_id: str | None = None

    # The same guards as the envelope, repeated on purpose. This is the type that
    # actually reaches the store, and it is constructible by any caller — not
    # only through ``from_envelope``. Validating only at the outer boundary would
    # leave the invariant depending on which door a record came through.
    @field_validator("subject")
    @classmethod
    def _subject_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("subject: must not be blank")
        return value

    @field_validator("features")
    @classmethod
    def _features_finite(cls, value: dict[str, float]) -> dict[str, float]:
        return {name: _finite(float(v), f"features[{name}]") for name, v in value.items()}

    @classmethod
    def from_envelope(cls, envelope: KnowledgeEnvelope) -> Observation:
        return cls(
            source=envelope.source,
            kind=envelope.kind,
            verification=envelope.verification,
            subject=envelope.subject,
            subject_type=envelope.subject_type,
            occurred_at=envelope.occurred_at,
            payload=dict(envelope.payload),
            features=dict(envelope.features),
            correlation_id=envelope.correlation_id,
        )


class Decision(ValueObject):
    """The evidence behind a decision, frozen at the instant it was taken.

    This is the half of the learning loop that has to be captured *early* or not
    at all. The obvious implementation — wait for the trade to close, then ask
    the feature store what the token looks like — trains the model on the state
    of the world at the moment of **sale**, labelled with the result of the
    trade. That is temporal leakage: it produces excellent validation metrics and
    a model that cannot work, because at decision time those features do not yet
    exist.

    So the vector is snapshotted here, when the decision is made, and never
    refreshed. ``ref`` is whatever downstream identifier will later be quoted
    back when the outcome lands (in practice the position's aggregate id) — the
    journal joins on it without needing to know what it names.
    """

    ref: str
    subject: str
    decided_at: datetime
    features: dict[str, float] = Field(default_factory=dict)
    #: The committee's probabilities and confidence as they stood, so a lesson
    #: can later be read as "what we believed" vs. "what happened".
    beliefs: dict[str, float] = Field(default_factory=dict)
    tags: dict[str, str] = Field(default_factory=dict)
    correlation_id: str | None = None

    @field_validator("ref", "subject")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class Outcome(ValueObject):
    """What actually happened to a decision. The other half of the loop."""

    ref: str
    settled_at: datetime
    realized_roi: float
    hit_take_profit: bool = False
    hit_stop_loss: bool = False
    reason: str = ""

    @field_validator("realized_roi")
    @classmethod
    def _roi_finite(cls, value: float) -> float:
        return _finite(value, "realized_roi")


class Lesson(ValueObject):
    """A decision married to its outcome — a training sample with ground truth.

    The features are the decision's, captured before the outcome existed; the
    labels are the outcome's. Nothing here is inferred after the fact, which is
    precisely what makes it usable for training.
    """

    ref: str
    subject: str
    decided_at: datetime
    settled_at: datetime
    features: dict[str, float]
    beliefs: dict[str, float] = Field(default_factory=dict)
    tags: dict[str, str] = Field(default_factory=dict)
    realized_roi: float
    label_roi_positive: bool
    label_hit_tp: bool
    label_hit_sl: bool
    reason: str = ""
    verification: Verification = Verification.REALISED
    correlation_id: str | None = None

    @property
    def holding_seconds(self) -> float:
        return max(0.0, (self.settled_at - self.decided_at).total_seconds())

    @classmethod
    def join(cls, decision: Decision, outcome: Outcome) -> Lesson:
        """Marry a decision to its outcome. The only way a Lesson is made."""
        return cls(
            ref=decision.ref,
            subject=decision.subject,
            decided_at=decision.decided_at,
            settled_at=outcome.settled_at,
            features=dict(decision.features),
            beliefs=dict(decision.beliefs),
            tags=dict(decision.tags),
            realized_roi=outcome.realized_roi,
            label_roi_positive=outcome.realized_roi > 0.0,
            label_hit_tp=outcome.hit_take_profit,
            label_hit_sl=outcome.hit_stop_loss,
            reason=outcome.reason,
            correlation_id=decision.correlation_id,
        )


class KnowledgeQuery(ValueObject):
    """Read-side filter. All fields optional; results newest-first."""

    source: KnowledgeSource | None = None
    kind: KnowledgeKind | None = None
    subject: str | None = None
    subject_type: SubjectType | None = None
    #: Minimum verification strength, compared through :data:`VERIFICATION_RANK`.
    min_verification: Verification | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = Field(default=100, ge=1, le=10_000)
    offset: int = Field(default=0, ge=0)

    def accepts(self, observation: Observation) -> bool:
        """Whether ``observation`` satisfies this filter (used by the in-memory store)."""
        if self.source is not None and observation.source is not self.source:
            return False
        if self.kind is not None and observation.kind is not self.kind:
            return False
        if self.subject is not None and observation.subject != self.subject:
            return False
        if self.subject_type is not None and observation.subject_type is not self.subject_type:
            return False
        if self.min_verification is not None and (
            VERIFICATION_RANK[observation.verification] < VERIFICATION_RANK[self.min_verification]
        ):
            return False
        if self.since is not None and observation.occurred_at < self.since:
            return False
        return not (self.until is not None and observation.occurred_at > self.until)


class KnowledgeStats(ValueObject):
    """A census of the memory, for the dashboard and for training readiness."""

    total: int = 0
    lessons: int = 0
    realised_lessons: int = 0
    positive_lessons: int = 0
    open_decisions: int = 0
    by_source: dict[str, int] = Field(default_factory=dict)
    by_kind: dict[str, int] = Field(default_factory=dict)

    @property
    def positive_rate(self) -> float:
        """Share of lessons with a positive return.

        This is the number that decides whether training is even possible: a
        dataset whose positive rate is 0.0 or 1.0 has one class, and a one-class
        dataset has an undefined AUC, which no validation gate can pass.
        """
        return (self.positive_lessons / self.lessons) if self.lessons else 0.0

    @property
    def is_trainable(self) -> bool:
        """Whether the memory holds both classes. Not "enough" — merely possible."""
        return self.lessons > 0 and 0 < self.positive_lessons < self.lessons


__all__ = [
    "VERIFICATION_RANK",
    "Decision",
    "KnowledgeEnvelope",
    "KnowledgeKind",
    "KnowledgeQuery",
    "KnowledgeSource",
    "KnowledgeStats",
    "Lesson",
    "Observation",
    "Outcome",
    "SubjectType",
    "Verification",
]
