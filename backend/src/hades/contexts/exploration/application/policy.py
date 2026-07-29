"""The exploration policy — pure, deterministic, and reproducible by hand.

This module is the whole of the decision. It does no I/O, holds no state and
consults no model: given a candidate, the evidence census and the spend to date,
it returns the same verdict every time, and a person with those three numbers can
recompute it on paper. That is a requirement rather than a nicety. A programme
that spends real capital to answer a question must be able to say, months later
and to someone who was not there, exactly why it bought each sample.

There is deliberately **no epsilon-greedy, no bandit, no randomness**. The
textbook exploration heuristics all decide *how often* to explore and leave
*which* candidate opaque; here the opposite is wanted. The frequency is already
pinned down by an explicit budget, so what remains is a selection rule, and the
one used is the simplest defensible thing: take the candidate whose cohort the
memory knows least about. Randomness would buy a marginally better-mixed sample
in exchange for a decision nobody can reconstruct, which is a bad trade for a
context whose entire output is evidence.

The order of the checks is itself a design decision, and it is about the
counters rather than the outcome (any failing check declines). Candidate-specific
checks run **before** budget checks, so a ``daily_budget`` decline means "this
candidate was worth sampling and the budget stopped it" — genuine budget
pressure — rather than being inflated by candidates that were never eligible.
Within the budget checks the most durable ceiling is reported first: a
``total_budget`` decline never resolves on its own, and an operator should not
have to wait a day to discover that.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hades.contexts.exploration.domain.models import (
    EvidenceStatus,
    ExplorationBudget,
    ExplorationCandidate,
    ExplorationConfig,
    ExplorationDecline,
    ExplorationSpend,
    ExplorationVerdict,
)


def day_start(now: datetime) -> datetime:
    """Midnight UTC of ``now``'s day — the daily budget window.

    UTC rather than a local timezone on purpose: the platform runs 24/7 in
    containers whose clocks are UTC, and a window that moved with a configured
    timezone would silently give one extra (or one fewer) budget reset on each
    daylight-saving boundary.
    """
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def week_start(now: datetime) -> datetime:
    """Midnight UTC of the ISO week's Monday — the weekly budget window."""
    start = day_start(now)
    return start - timedelta(days=start.weekday())


class ExplorationPolicy:
    """Decides whether one candidate earns an exploration trade. Pure."""

    def __init__(self, config: ExplorationConfig) -> None:
        self._cfg = config

    @property
    def config(self) -> ExplorationConfig:
        return self._cfg

    def decide(
        self,
        candidate: ExplorationCandidate,
        evidence: EvidenceStatus,
        spend: ExplorationSpend,
        *,
        now: datetime,
    ) -> ExplorationVerdict:
        cfg = self._cfg
        reasons: list[str] = []

        if not cfg.enabled:
            return self._decline(candidate, ExplorationDecline.DISABLED, evidence, spend, now, ())

        # The memory could not be read. Not knowing whether more evidence is
        # needed is a reason to stop spending, never to keep going.
        if not evidence.available:
            return self._decline(
                candidate,
                ExplorationDecline.UNAVAILABLE,
                evidence,
                spend,
                now,
                ("permanent memory unreadable — exploration fails closed",),
            )

        # The terminal condition, checked before anything else can spend: the
        # programme has bought what it existed to buy.
        if evidence.sufficient:
            return self._decline(
                candidate,
                ExplorationDecline.EVIDENCE_SUFFICIENT,
                evidence,
                spend,
                now,
                (f"evidence sufficient — {evidence.summary}",),
            )

        # -- candidate-specific checks ----------------------------------------

        prob = candidate.prob_roi_positive
        if prob < cfg.min_prob_roi_positive:
            return self._decline(
                candidate,
                ExplorationDecline.OUTSIDE_BAND,
                evidence,
                spend,
                now,
                (
                    f"P(ROI+) {prob:.2f} below the exploration floor "
                    f"{cfg.min_prob_roi_positive:.2f} — an unattractive candidate is not "
                    "an open question",
                ),
            )
        if prob > cfg.max_prob_roi_positive:
            return self._decline(
                candidate,
                ExplorationDecline.OUTSIDE_BAND,
                evidence,
                spend,
                now,
                (
                    f"P(ROI+) {prob:.2f} above the exploration ceiling "
                    f"{cfg.max_prob_roi_positive:.2f} — the production path judges this one",
                ),
            )
        if candidate.confidence < cfg.min_confidence:
            return self._decline(
                candidate,
                ExplorationDecline.OUTSIDE_BAND,
                evidence,
                spend,
                now,
                (
                    f"confidence {candidate.confidence:.2f} below the exploration floor "
                    f"{cfg.min_confidence:.2f}",
                ),
            )
        reasons.append(
            f"P(ROI+) {prob:.2f} inside the exploration band "
            f"[{cfg.min_prob_roi_positive:.2f}, {cfg.max_prob_roi_positive:.2f}]"
        )

        cohort_key, cohort_count = self._least_sampled_cohort(candidate, evidence)
        if cohort_key is not None and cohort_count >= cfg.target.cohort_target:
            return self._decline(
                candidate,
                ExplorationDecline.COHORT_SATURATED,
                evidence,
                spend,
                now,
                (
                    f"every cohort of this candidate is sampled to target "
                    f"(least-known is {cohort_key} at {cohort_count} lessons "
                    f">= {cfg.target.cohort_target})",
                ),
            )
        if cohort_key is None:
            # No attribution at all. It still buys a sample of the global
            # population, which is short by definition while evidence is
            # insufficient — and the budget bounds how many such trades exist.
            reasons.append(
                "no cohort attribution — sampled against the global population "
                f"({evidence.lessons} lessons)"
            )
        else:
            reasons.append(
                f"cohort {cohort_key} known from {cohort_count} settled lessons "
                f"(target {cfg.target.cohort_target})"
            )

        # -- programme-level budget checks ------------------------------------

        decline = self._budget_decline(cfg.budget, spend)
        if decline is not None:
            return self._decline(candidate, decline[0], evidence, spend, now, (decline[1],))

        reasons.append(f"evidence still short — {evidence.summary}")
        reasons.append(
            f"fixed exploration size ${cfg.budget.per_trade_usd:.4f}, not conviction-weighted"
        )
        return ExplorationVerdict(
            granted=True,
            subject=candidate.subject,
            notional_usd=cfg.budget.per_trade_usd,
            stop_loss_pct=cfg.stop_loss_pct,
            take_profit_pct=cfg.take_profit_pct,
            cohort_key=cohort_key,
            cohort_count=cohort_count,
            reasons=tuple(reasons),
            evidence_summary=evidence.summary,
            budget_summary=self._budget_summary(cfg.budget, spend),
            at=now,
        )

    # -- internals ------------------------------------------------------------

    def _least_sampled_cohort(
        self, candidate: ExplorationCandidate, evidence: EvidenceStatus
    ) -> tuple[str | None, int]:
        """The candidate's cohort the memory knows least about.

        Ties break on the key's own name so the choice is stable across runs and
        across processes: two workers looking at the same candidate must justify
        it identically, or the audit trail disagrees with itself.
        """
        scored: list[tuple[int, str]] = []
        for dimension in self._cfg.cohort_dimensions:
            value = candidate.cohorts.get(dimension)
            if not value:
                continue
            key = f"{dimension}={value}"
            scored.append((evidence.cohort_count(key), key))
        if not scored:
            return None, evidence.lessons
        scored.sort()
        count, key = scored[0]
        return key, count

    @staticmethod
    def _budget_decline(
        budget: ExplorationBudget, spend: ExplorationSpend
    ) -> tuple[ExplorationDecline, str] | None:
        """The first ceiling this trade would breach, most durable first."""
        size = budget.per_trade_usd
        if spend.total_usd + size > budget.total_usd:
            return (
                ExplorationDecline.TOTAL_BUDGET,
                f"lifetime budget spent: ${spend.total_usd:.2f} + ${size:.2f} "
                f"> ${budget.total_usd:.2f} — this ceiling does not reset",
            )
        if spend.week_usd + size > budget.weekly_usd:
            return (
                ExplorationDecline.WEEKLY_BUDGET,
                f"weekly budget spent: ${spend.week_usd:.2f} + ${size:.2f} "
                f"> ${budget.weekly_usd:.2f}",
            )
        if spend.week_trades >= budget.max_trades_per_week:
            return (
                ExplorationDecline.WEEKLY_TRADES,
                f"weekly trade cap reached: {spend.week_trades} "
                f">= {budget.max_trades_per_week}",
            )
        if spend.day_usd + size > budget.daily_usd:
            return (
                ExplorationDecline.DAILY_BUDGET,
                f"daily budget spent: ${spend.day_usd:.2f} + ${size:.2f} "
                f"> ${budget.daily_usd:.2f}",
            )
        if spend.day_trades >= budget.max_trades_per_day:
            return (
                ExplorationDecline.DAILY_TRADES,
                f"daily trade cap reached: {spend.day_trades} >= {budget.max_trades_per_day}",
            )
        return None

    @staticmethod
    def _budget_summary(budget: ExplorationBudget, spend: ExplorationSpend) -> str:
        left = spend.remaining(budget)
        return (
            f"day ${spend.day_usd:.2f}/${budget.daily_usd:.2f} "
            f"({int(left['day_trades'])} trades left), "
            f"week ${spend.week_usd:.2f}/${budget.weekly_usd:.2f}, "
            f"total ${spend.total_usd:.2f}/${budget.total_usd:.2f}"
        )

    def _decline(
        self,
        candidate: ExplorationCandidate,
        reason: ExplorationDecline,
        evidence: EvidenceStatus,
        spend: ExplorationSpend,
        now: datetime,
        reasons: tuple[str, ...],
    ) -> ExplorationVerdict:
        return ExplorationVerdict(
            granted=False,
            subject=candidate.subject,
            decline=reason,
            reasons=reasons,
            evidence_summary=evidence.summary,
            budget_summary=self._budget_summary(self._cfg.budget, spend),
            at=now,
        )


__all__ = ["ExplorationPolicy", "day_start", "week_start"]
