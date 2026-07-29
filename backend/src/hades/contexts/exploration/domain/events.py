"""Domain events emitted by Exploration — the programme's public record.

None of these is a trade instruction, and none can become one: an
``ExplorationGranted`` says a candidate became *eligible* for the Risk Manager to
consider under exploration rules, which is a strictly weaker statement than any
approval. The Execution Engine does not subscribe to this context and could not
act on any of it if it did.

The events exist for three readers. The **Knowledge** context records them, so
the programme's own history is part of permanent memory and a lesson can later be
attributed to the exploration budget that paid for it. The **notification**
runtime turns the two consequential ones — budget exhausted, programme complete —
into an operator alert. And the **audit** trail keeps them because a programme
that spends money to answer a question should be able to show, afterwards,
exactly what it spent and what question it was answering at the time.

``ExplorationCompleted`` is the one to watch: it is the platform saying it has
finished buying evidence and has switched the programme off by itself.
"""

from __future__ import annotations

from hades.shared_kernel.domain.events import DomainEvent


class ExplorationGranted(DomainEvent):
    """A candidate became eligible for an exploration trade.

    Eligibility, not approval — the Risk Manager still runs every allocation
    rule afterwards and may reject. The event carries the arithmetic that
    justified the grant so the decision is reconstructable months later without
    this code in front of you.
    """

    aggregate_type: str = "exploration"
    subject: str
    notional_usd: float
    cohort_key: str | None = None
    cohort_count: int = 0
    prob_roi_positive: float = 0.0
    confidence: float = 0.0
    evidence_summary: str = ""
    budget_summary: str = ""
    reasons: tuple[str, ...] = ()
    correlation_id_ref: str | None = None


class ExplorationSpent(DomainEvent):
    """An exploration grant was approved by Risk and charged to the budget.

    Separate from :class:`ExplorationGranted` because the two differ exactly when
    it matters: a grant that Risk then rejects costs nothing, and a programme
    that reported spending on every grant would overstate its burn and understate
    its remaining runway.
    """

    aggregate_type: str = "exploration"
    subject: str
    notional_usd: float
    spent_day_usd: float = 0.0
    spent_week_usd: float = 0.0
    spent_total_usd: float = 0.0
    trades_total: int = 0
    correlation_id_ref: str | None = None


class ExplorationBudgetExhausted(DomainEvent):
    """A budget ceiling was reached and exploration stopped granting.

    ``window`` is ``daily`` / ``weekly`` / ``total``. The first two clear on
    their own; the last does not, and an operator seeing it should read it as the
    programme having spent its full allowance without reaching sufficiency.
    """

    aggregate_type: str = "exploration"
    window: str
    spent_usd: float
    limit_usd: float
    detail: str = ""


class ExplorationCompleted(DomainEvent):
    """The memory reached sufficiency: exploration switched **itself** off.

    This is the success condition of the whole programme, and the reason it can
    be run without a calendar reminder to turn it off. The context latches on
    this and will not grant again in the process's lifetime, whatever later
    queries return.
    """

    aggregate_type: str = "exploration"
    lessons: int
    positive: int
    negative: int
    detail: str = ""


__all__ = [
    "ExplorationBudgetExhausted",
    "ExplorationCompleted",
    "ExplorationGranted",
    "ExplorationSpent",
]
