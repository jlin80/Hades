"""Model Comparator + Strategy Comparator.

The lab is a permanent tournament. The Strategy Comparator ranks strategies on the
whole scorecard (win rate, expectancy, profit factor, Sharpe, Sortino, recovery,
max drawdown, consistency, avg holding). The Model Comparator lines up the
production, candidate and shadow models on classification *and* trading metrics and
reports the deltas plus a plain-English verdict.

Comparisons are evidence, not decisions — a favourable delta recommends nothing on
its own; promotion stays a separate, human-gated step.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from hades.contexts.research.application import metrics as m
from hades.contexts.research.domain.models import (
    ComparisonEntry,
    ModelComparison,
    PerformanceMetrics,
    StrategyComparison,
    TradeRecord,
)

# Metric → whether higher is better, used for ranking.
_HIGHER_BETTER = {
    "expectancy": True,
    "profit_factor": True,
    "sharpe": True,
    "sortino": True,
    "win_rate": True,
    "recovery_factor": True,
    "consistency": True,
    "total_return": True,
    "max_drawdown": False,
}


class StrategyComparator:
    """Ranks strategies on a chosen objective, tie-broken by the full scorecard."""

    def compare(
        self,
        scorecards: Mapping[str, PerformanceMetrics],
        *,
        objective: str = "expectancy",
    ) -> StrategyComparison:
        entries = tuple(
            ComparisonEntry(label=label, metrics=metrics) for label, metrics in scorecards.items()
        )

        def sort_key(entry: ComparisonEntry) -> tuple[float, float, float]:
            primary = getattr(entry.metrics, objective, 0.0)
            if not _HIGHER_BETTER.get(objective, True):
                primary = -primary
            return (
                float(primary),
                entry.metrics.sharpe,
                -entry.metrics.max_drawdown,
            )

        ranked = sorted(entries, key=sort_key, reverse=True)
        return StrategyComparison(
            entries=entries,
            ranking=tuple(e.label for e in ranked),
            objective=objective,
        )


class ModelComparator:
    """Compares production/candidate/shadow model metrics and states a verdict."""

    def compare(
        self,
        *,
        production: Mapping[str, float],
        candidate: Mapping[str, float],
        shadow: Mapping[str, float] | None = None,
    ) -> ModelComparison:
        keys = set(production) | set(candidate)
        deltas = {
            k: round(float(candidate.get(k, 0.0)) - float(production.get(k, 0.0)), 6) for k in keys
        }
        # A candidate beats the incumbent when the headline metrics improve without
        # a worse drawdown. This is a *recommendation*, never an activation.
        improved = [
            k for k in ("auc", "roi", "sharpe", "precision", "recall") if deltas.get(k, 0.0) > 0
        ]
        dd_worse = deltas.get("max_drawdown", 0.0) > 0.0
        if improved and not dd_worse:
            verdict = f"candidate improves {', '.join(improved)} — eligible for shadow/validation"
        elif improved:
            verdict = "candidate improves some metrics but drawdown worsened — inconclusive"
        else:
            verdict = "candidate does not beat production — keep incumbent"
        return ModelComparison(
            production=dict(production),
            candidate=dict(candidate),
            shadow=dict(shadow or {}),
            deltas=deltas,
            verdict=verdict,
        )


def scorecards_from_trades(
    labelled: Sequence[tuple[str, Sequence[TradeRecord]]],
) -> dict[str, PerformanceMetrics]:
    """Helper: build a label→scorecard map from labelled trade lists."""
    return {label: m.compute(trades) for label, trades in labelled}
