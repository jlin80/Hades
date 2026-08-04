"""Exploration Prometheus metrics — one declaration site for the context.

The two gauges worth alerting on are ``active`` and ``budget_total_remaining``.
Together they answer the only question an operator has about this programme:
*is it still running, and how much runway is left?* A programme that is inactive
with runway remaining has either finished (the success case, which also emits
``ExplorationCompleted``) or been switched off; a programme that is active with
the runway falling and ``evidence_lessons`` flat is spending without learning,
which is the failure mode that matters and the one the counters make visible
within an hour instead of after a week.

``declined_total`` is labelled by cause deliberately. A single undifferentiated
decline counter cannot distinguish "the day's allowance is spent" — which fixes
itself overnight — from "every candidate falls outside the band", which means the
band is misconfigured and no amount of waiting will produce a trade.
"""

from __future__ import annotations

from hades.shared_kernel.observability import MetricsRegistry


class ExplorationMetrics:
    """Typed accessors over the shared metrics registry for Exploration."""

    def __init__(self, metrics: MetricsRegistry) -> None:
        self.granted = metrics.counter(
            "hades_exploration_granted_total",
            "Candidates found eligible for an exploration trade",
        )
        self.declined = metrics.counter(
            "hades_exploration_declined_total",
            "Candidates refused an exploration trade, by cause",
            ("reason",),
        )
        self.spent = metrics.counter(
            "hades_exploration_spent_usd_total",
            "Capital committed to exploration trades (charged on approval)",
        )
        self.errors = metrics.counter(
            "hades_exploration_errors_total",
            "Failures reading evidence or the exploration ledger",
        )
        self.active = metrics.gauge(
            "hades_exploration_active",
            "1 while exploration is granting trades, 0 once it is not",
        )
        self.evidence_lessons = metrics.gauge(
            "hades_exploration_evidence_lessons",
            "Settled lessons the sufficiency test is counting",
        )
        self.evidence_positive = metrics.gauge(
            "hades_exploration_evidence_positive",
            "Settled lessons with a positive return (a class the gate requires)",
        )
        self.evidence_negative = metrics.gauge(
            "hades_exploration_evidence_negative",
            "Settled lessons with a non-positive return (the other required class)",
        )
        self.budget_day_remaining = metrics.gauge(
            "hades_exploration_budget_day_remaining_usd",
            "Unspent portion of today's exploration budget",
        )
        self.budget_total_remaining = metrics.gauge(
            "hades_exploration_budget_total_remaining_usd",
            "Unspent portion of the programme's lifetime exploration budget",
        )


__all__ = ["ExplorationMetrics"]
