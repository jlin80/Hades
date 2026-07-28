"""Parameter Optimizer — searches genome parameters under a multi-objective score.

Optimises the knobs that matter (stop-loss, take-profit, entry threshold, holding
time…) but **never optimises ROI alone**. The objective blends return with
drawdown, Sharpe, Sortino, profit factor and expectancy (see
``metrics.multi_objective_score``) so the winner is robust rather than curve-fit.

Uses a deterministic random search (seeded) over a bounded grid — no SciPy, so it
runs on the low-power CPUs the platform targets. It only *proposes* parameters;
nothing is applied to production.
"""

from __future__ import annotations

import random
from collections.abc import Sequence

from hades.contexts.research.application import metrics as m
from hades.contexts.research.application.backtest_engine import BacktestingEngine
from hades.contexts.research.domain.models import (
    MarketConditions,
    OptimizationResult,
    StrategyGenome,
)
from hades.contexts.research.domain.ports import HistoricalSample

# Bounded search space for the tunable knobs (inclusive ranges).
_SPACE: dict[str, tuple[float, float]] = {
    "take_profit": (0.10, 0.60),
    "stop_loss": (0.05, 0.30),
    "entry_score": (0.45, 0.80),
    "max_holding_seconds": (300.0, 7200.0),
}


class ParameterOptimizer:
    """Seeded multi-objective random search over a genome's parameters."""

    def __init__(
        self,
        backtester: BacktestingEngine | None = None,
        *,
        seed: int = 11,
    ) -> None:
        self._bt = backtester or BacktestingEngine()
        self._seed = seed

    def optimize(
        self,
        *,
        strategy: str,
        genome: StrategyGenome,
        samples: Sequence[HistoricalSample],
        evaluations: int = 40,
        objective: str = "multi_objective",
        conditions: MarketConditions | None = None,
        weights: dict[str, float] | None = None,
    ) -> OptimizationResult:
        conditions = conditions or MarketConditions()
        rng = random.Random(self._seed)

        best_params: dict[str, float] = {}
        best_score = float("-inf")
        best_metrics = m.compute([])
        trials: list[dict[str, float]] = []

        for _ in range(max(1, evaluations)):
            params = {key: round(rng.uniform(lo, hi), 4) for key, (lo, hi) in _SPACE.items()}
            candidate = genome.model_copy(
                update={
                    "take_profit": params["take_profit"],
                    "stop_loss": params["stop_loss"],
                    "entry_score": params["entry_score"],
                    "max_holding_seconds": params["max_holding_seconds"],
                }
            )
            result = self._bt.run(
                strategy=strategy, genome=candidate, samples=samples, conditions=conditions
            )
            score = m.multi_objective_score(result.metrics, weights=weights)
            trials.append({**params, "score": score})
            if score > best_score:
                best_score = score
                best_params = params
                best_metrics = result.metrics

        return OptimizationResult(
            strategy=strategy,
            objective=objective,
            best_params=best_params,
            best_score=round(best_score, 6) if best_score != float("-inf") else 0.0,
            best_metrics=best_metrics,
            evaluations=len(trials),
            trials=tuple(trials),
        )
