"""Decision Backtest — replays history through the *production* decision path.

Checklist §3. The distinction from :mod:`backtest_engine` is the whole point and
is worth stating plainly: that engine scores :class:`StrategyGenome` archetypes,
which are its own pure signal functions over its own normalised channels. It is a
second, independent decision system that happens to live in the same repository.
Whatever it concludes is a statement about *it*, not about what Hades would do.

This engine takes the opposite approach. It composes the **real**
:class:`CommitteeManager` and the **real** :class:`PositionSizingEngine` and risk
policy chain, and drives them over stored history. There is no copied threshold,
no re-implemented scorer and no parallel model: change a specialist's weights or
a risk policy and this backtest changes with it, because it is running the same
objects production runs.

**Why it lives in `ops` and not in the Research Lab.** The lab is forbidden by an
AST test from importing Execution, Risk or Portfolio, and that ban is the entire
safety argument for it — a context that cannot import Risk cannot be argued into
trading. Composing the real sizing engine therefore *cannot* happen inside
``contexts/research``. It happens here, in the composition root, which is already
where every context is wired together and is the one layer allowed to see them
all. The lab keeps what it can honestly own: read-only access to history, via
:class:`OutcomeDecisionHistoryReader`.

**It cannot trade, and the reason is structural rather than careful.**
``TradeApproved`` is constructed in exactly one place in the platform — the Risk
Manager — and this module does not use the Risk Manager. It uses the sizing
engine and the policy chain, which compute a verdict and a size but carry no
authority to act, and it is handed an isolated in-memory event bus and prediction
store rather than the production ones. There is no executor here, no portfolio
and no wallet, so a replay has nothing to act *through*.

**What it can and cannot measure today.** It measures the decision path: how many
historical candidates clear the committee's gates, at what size, and which policy
rejected the rest. It does **not** measure returns, because measuring returns
needs labelled executed outcomes and the platform has none yet — the outcome
ledger currently holds only rejections. Reporting a P&L from that would be
inventing one. Buying those labels is what the Exploration programme (§6p) exists
to do, and until it has, this engine answers "what would we decide?" and not
"what would we have earned".
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from hades.contexts.common.domain.value_objects import TokenMint, TokenRef
from hades.contexts.learning.application.committee.manager import CommitteeManager
from hades.contexts.learning.domain.models import (
    CandidateEnrichment,
    CandidateIdentity,
    DecisionContext,
    EnrichedCandidate,
    NormalizedVector,
)
from hades.contexts.research.domain.ports import DecisionHistoryReader
from hades.contexts.risk.application.sizing import PositionSizingEngine
from hades.contexts.risk.domain.models import (
    CapitalSnapshot,
    PortfolioRiskState,
    RiskCandidate,
)
from hades.contexts.risk.domain.ports import RiskPolicy
from hades.shared_kernel.logging import get_logger

_logger = get_logger("ops.decision_backtest")

#: One stored historical row: the feature vector plus whatever labels exist.
HistoricalDecision = dict[str, object]


@dataclass
class DecisionOutcome:
    """What the production decision path did with one historical candidate."""

    mint: str
    at: datetime
    prob_roi_positive: float = 0.0
    confidence: float = 0.0
    approved: bool = False
    notional_usd: float = 0.0
    conviction: float = 0.0
    #: The first policy that rejected it, or the sizing engine's own reason.
    rejected_by: str = ""
    detail: str = ""
    #: Carried through from the stored row when present. Never synthesised.
    label_roi_positive: bool | None = None
    realized_roi: float | None = None


@dataclass
class DecisionBacktestReport:
    """The aggregate. Every field is a count of something actually observed."""

    evaluated: int = 0
    approved: int = 0
    rejected: int = 0
    errors: int = 0
    rejections_by_cause: dict[str, int] = field(default_factory=dict)
    outcomes: list[DecisionOutcome] = field(default_factory=list)
    #: How many rows carried a usable realised label. Zero means no return
    #: statistic in this report means anything, and none is offered.
    labelled: int = 0

    @property
    def approval_rate(self) -> float:
        return self.approved / self.evaluated if self.evaluated else 0.0

    @property
    def total_notional_usd(self) -> float:
        return round(sum(o.notional_usd for o in self.outcomes if o.approved), 6)

    @property
    def can_measure_returns(self) -> bool:
        """True only with labelled rows of *both* classes.

        A single-class set cannot support a return statistic any more than it can
        support an AUC, and the platform's ledger is currently single-class by
        construction. Guarding on it here stops a future caller from reading a
        mean of 1,771 rejections as performance.
        """
        if self.labelled == 0:
            return False
        labels = {o.label_roi_positive for o in self.outcomes if o.label_roi_positive is not None}
        return len(labels) > 1

    def summary(self) -> dict[str, object]:
        return {
            "evaluated": self.evaluated,
            "approved": self.approved,
            "rejected": self.rejected,
            "errors": self.errors,
            "approval_rate": round(self.approval_rate, 4),
            "total_notional_usd": self.total_notional_usd,
            "rejections_by_cause": dict(self.rejections_by_cause),
            "labelled": self.labelled,
            "can_measure_returns": self.can_measure_returns,
        }


class DecisionBacktest:
    """Replays stored history through the production committee + risk path."""

    def __init__(
        self,
        *,
        committee: CommitteeManager,
        sizing: PositionSizingEngine,
        policies: Sequence[RiskPolicy] = (),
        equity_usd: float = 1_000.0,
        available_usd: float | None = None,
    ) -> None:
        self._committee = committee
        self._sizing = sizing
        self._policies = tuple(policies)
        self._equity = equity_usd
        self._available = available_usd if available_usd is not None else equity_usd

    async def run(self, rows: Sequence[HistoricalDecision]) -> DecisionBacktestReport:
        report = DecisionBacktestReport()
        for row in rows:
            try:
                outcome = await self._evaluate(row)
            except Exception as exc:
                # One malformed historical row must not end a replay of 1,771.
                report.errors += 1
                _logger.warning("decision_backtest_row_failed", error=str(exc))
                continue
            report.evaluated += 1
            report.outcomes.append(outcome)
            if outcome.label_roi_positive is not None:
                report.labelled += 1
            if outcome.approved:
                report.approved += 1
            else:
                report.rejected += 1
                cause = outcome.rejected_by or "unknown"
                report.rejections_by_cause[cause] = report.rejections_by_cause.get(cause, 0) + 1
        _logger.info("decision_backtest_done", **report.summary())
        return report

    async def run_history(
        self,
        reader: DecisionHistoryReader,
        *,
        from_iso: str | None = None,
        to_iso: str | None = None,
        limit: int = 100_000,
    ) -> DecisionBacktestReport:
        """Replay a window of the platform's own stored history (§3.2).

        The reader is the only inbound dependency and it is read-only, so loading
        history can no more mutate production than the replay itself can trade.
        """
        rows = await reader.load_decisions(from_iso=from_iso, to_iso=to_iso, limit=limit)
        _logger.info("decision_backtest_loaded", rows=len(rows), from_iso=from_iso, to_iso=to_iso)
        return await self.run(list(rows))

    # -- one row --------------------------------------------------------------

    async def _evaluate(self, row: HistoricalDecision) -> DecisionOutcome:
        mint = str(row.get("mint", ""))
        at = _as_datetime(row.get("at"))
        features = _as_features(row.get("features"))
        token = TokenRef(mint=TokenMint(address=mint), symbol=None)

        prediction = await self._committee.evaluate(
            EnrichedCandidate(
                context=DecisionContext(
                    token=token,
                    at=at,
                    vector=NormalizedVector(
                        token=token,
                        at=at,
                        values=features,
                        raw=features,
                        # Coverage is the share of the catalog actually present.
                        # Stated from the row rather than assumed to be 1.0: a
                        # thin historical vector must lower confidence here for
                        # exactly the reason it would in production.
                        coverage=_coverage(features),
                        present=tuple(sorted(features)),
                    ),
                    security_score=float(features.get("security.score", 50.0)),
                ),
                # An empty enrichment, deliberately. The memory's priors are a
                # function of *when* a decision was made, and replaying today's
                # memory into a decision from last month is look-ahead bias — the
                # exact defect the audit flagged as R2. Neutral enrichment is the
                # only honest choice until lessons carry a usable as-of index.
                enrichment=CandidateEnrichment(identity=CandidateIdentity(mint=mint)),
            )
        )

        outcome = DecisionOutcome(
            mint=mint,
            at=at,
            prob_roi_positive=prediction.meta.prob_roi_positive,
            confidence=prediction.confidence.final,
            label_roi_positive=_as_bool(row.get("label_roi_positive")),
            realized_roi=_as_optional_float(row.get("realized_roi")),
        )

        candidate = RiskCandidate(
            token=token,
            at=at,
            prob_roi_positive=prediction.meta.prob_roi_positive,
            prob_hit_tp=prediction.meta.prob_hit_tp,
            prob_hit_sl=prediction.meta.prob_hit_sl,
            confidence=prediction.confidence.final,
            # The same projection the production context builder makes
            # (context_builder.py:53). Reading it any other way here would be a
            # second implementation of the mapping, which is the class of drift
            # this whole module exists to avoid.
            regime=prediction.regime.regime.value,
            security_score=float(features.get("security.score", 50.0)),
            liquidity_usd=float(features.get("basic.liquidity", 0.0)),
            volatility_pct=float(features.get("regime.volatility_recent", 0.0)),
        )

        decision = self._sizing.size(
            candidate, equity_usd=self._equity, available_usd=self._available
        )
        outcome.conviction = decision.conviction
        if not decision.approved or decision.sizing is None:
            outcome.rejected_by = "sizing"
            outcome.detail = decision.detail
            return outcome

        notional = float(decision.sizing.notional.amount)
        state = PortfolioRiskState(
            capital=CapitalSnapshot(
                equity_usd=self._equity,
                cash_usd=self._available,
                invested_usd=max(0.0, self._equity - self._available),
                # No reserve withheld in a replay: available_usd is already the
                # deployable figure the caller chose, and applying the reserve a
                # second time here would reject candidates production would take.
                reserve_pct=0.0,
            )
        )
        for policy in self._policies:
            verdict = policy.evaluate(candidate, state, notional)
            if not verdict.passed:
                outcome.rejected_by = policy.name
                outcome.detail = verdict.detail
                return outcome

        outcome.approved = True
        outcome.notional_usd = notional
        outcome.detail = decision.detail
        return outcome


# -- coercions ----------------------------------------------------------------
# Historical rows come from JSONB, so every field is "probably the right type".


def _as_features(value: object) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, float] = {}
    for key, raw in value.items():
        try:
            number = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            out[str(key)] = number
    return out


def _coverage(features: dict[str, float]) -> float:
    """Share of a nominal 100-feature catalog present, capped at 1.0."""
    return min(1.0, len(features) / 100.0) if features else 0.0


def _as_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return datetime.now(UTC)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)


def _as_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _as_optional_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


__all__ = [
    "DecisionBacktest",
    "DecisionBacktestReport",
    "DecisionOutcome",
    "HistoricalDecision",
]
