"""Monte-Carlo Engine — robustness under thousands of perturbed scenarios.

A single backtest is one path through history; luck can flatter it. Monte-Carlo
resamples and perturbs that path thousands of times — jittering slippage and
latency, shuffling trade order, dropping trades to model missed fills — and
reports the *distribution* of outcomes. A strategy whose 5th-percentile path is a
disaster is fragile no matter how good its mean looks.

Deterministic given a seed, so a reported robustness score is always reproducible.
"""

from __future__ import annotations

import random
from collections.abc import Sequence

from hades.contexts.research.application import metrics as m
from hades.contexts.research.domain.models import (
    MonteCarloResult,
    TradeRecord,
    clamp01,
    safe_div,
)


def _percentile(sorted_xs: Sequence[float], q: float) -> float:
    if not sorted_xs:
        return 0.0
    idx = round(q * (len(sorted_xs) - 1))
    return sorted_xs[max(0, min(len(sorted_xs) - 1, idx))]


class MonteCarloEngine:
    """Perturbs a strategy's realised trades to estimate robustness."""

    def __init__(self, *, seed: int = 7) -> None:
        self._seed = seed

    def run(
        self,
        *,
        strategy: str,
        trades: Sequence[TradeRecord],
        simulations: int = 1000,
        slippage_jitter: float = 0.01,
        drop_probability: float = 0.05,
    ) -> MonteCarloResult:
        base_returns = [t.ret for t in trades]
        if not base_returns:
            return MonteCarloResult(strategy=strategy, simulations=0)

        rng = random.Random(self._seed)
        total_returns: list[float] = []
        drawdowns: list[float] = []
        profitable = 0

        for _ in range(max(1, simulations)):
            path: list[float] = []
            for r in base_returns:
                if rng.random() < drop_probability:
                    continue  # a missed / failed fill
                jitter = rng.uniform(-slippage_jitter, slippage_jitter)
                path.append(r + jitter)
            rng.shuffle(path)  # order-dependence stress
            curve = m.equity_curve(path)
            total = curve[-1] - 1.0
            total_returns.append(total)
            drawdowns.append(m.max_drawdown(curve))
            if total > 0:
                profitable += 1

        total_returns.sort()
        mean = safe_div(sum(total_returns), len(total_returns))
        std = (
            (sum((x - mean) ** 2 for x in total_returns) / (len(total_returns) - 1)) ** 0.5
            if len(total_returns) > 1
            else 0.0
        )
        prob_profit = safe_div(profitable, len(total_returns))
        worst_dd = max(drawdowns) if drawdowns else 0.0
        # Composite robustness: likely to profit, shallow tail loss, low dispersion.
        p05 = _percentile(total_returns, 0.05)
        robustness = clamp01(
            0.5 * prob_profit + 0.3 * clamp01(1.0 + min(0.0, p05)) + 0.2 * clamp01(1.0 - worst_dd)
        )
        return MonteCarloResult(
            strategy=strategy,
            simulations=len(total_returns),
            mean_return=round(mean, 6),
            median_return=round(_percentile(total_returns, 0.5), 6),
            std_return=round(std, 6),
            p05_return=round(p05, 6),
            p95_return=round(_percentile(total_returns, 0.95), 6),
            worst_drawdown=round(worst_dd, 6),
            prob_profit=round(prob_profit, 6),
            robustness=round(robustness, 6),
        )
