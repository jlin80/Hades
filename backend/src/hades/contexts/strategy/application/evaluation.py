"""SelfEvaluator - turns realised outcomes into a strategy scorecard.

Pure statistics over the outcomes a strategy actually produced (attributed from
the portfolio Position stream). Computes everything the spec's Self-Evaluation
section names - win rate, profit factor, Sharpe, Sortino, recovery, average win /
loss, holding time and expectancy - and derives a ``degraded`` flag the weighting
engine uses to *taper* (never delete) a slipping strategy's weight.
"""

from __future__ import annotations

import math

from hades.contexts.strategy.domain.models import StrategyOutcome, StrategyPerformance


class SelfEvaluator:
    """Computes a :class:`StrategyPerformance` from a strategy's outcomes."""

    def __init__(
        self,
        *,
        min_trades_for_degrade: int = 8,
        degrade_profit_factor: float = 1.0,
        degrade_win_rate: float = 0.35,
    ) -> None:
        self._min_trades = max(1, min_trades_for_degrade)
        self._degrade_pf = degrade_profit_factor
        self._degrade_wr = degrade_win_rate

    def evaluate(self, strategy: str, outcomes: list[StrategyOutcome]) -> StrategyPerformance:
        if not outcomes:
            return StrategyPerformance(strategy=strategy)

        pnls = [o.realized_pnl_usd for o in outcomes]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        trades = len(pnls)

        win_rate = len(wins) / trades
        profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (gross_win or 1.0)
        avg_win = (gross_win / len(wins)) if wins else 0.0
        avg_loss = (gross_loss / len(losses)) if losses else 0.0
        expectancy = sum(pnls) / trades
        avg_holding = sum(o.holding_seconds for o in outcomes) / trades

        sharpe = _sharpe(pnls)
        sortino = _sortino(pnls)
        max_dd = _max_drawdown(pnls)
        net = sum(pnls)
        recovery = (net / max_dd) if max_dd > 0 else (net if net > 0 else 0.0)

        degraded = trades >= self._min_trades and (
            profit_factor < self._degrade_pf or win_rate < self._degrade_wr
        )

        return StrategyPerformance(
            strategy=strategy,
            trades=trades,
            wins=len(wins),
            losses=len(losses),
            win_rate=round(win_rate, 4),
            profit_factor=round(profit_factor, 4),
            sharpe=round(sharpe, 4),
            sortino=round(sortino, 4),
            recovery_factor=round(recovery, 4),
            avg_win_usd=round(avg_win, 4),
            avg_loss_usd=round(avg_loss, 4),
            avg_holding_seconds=round(avg_holding, 2),
            expectancy_usd=round(expectancy, 4),
            max_drawdown_usd=round(max_dd, 4),
            net_pnl_usd=round(net, 4),
            degraded=degraded,
        )


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    mu = _mean(xs)
    var = sum((x - mu) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(var)


def _sharpe(pnls: list[float]) -> float:
    sd = _std(pnls)
    return (_mean(pnls) / sd) if sd > 0 else 0.0


def _sortino(pnls: list[float]) -> float:
    downside = [min(0.0, p) for p in pnls]
    dd = _std(downside) if any(d < 0 for d in downside) else 0.0
    return (_mean(pnls) / dd) if dd > 0 else 0.0


def _max_drawdown(pnls: list[float]) -> float:
    """Peak-to-trough drawdown of the cumulative PnL curve (USD, positive)."""
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cumulative += p
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)
    return max_dd
