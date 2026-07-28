"""Shadow Strategy Engine — live-shaped strategies that are entirely virtual.

A shadow strategy sees exactly what a production strategy sees (the same feature
vectors, in real time) and behaves identically — opening and closing positions,
tracking P&L — with one absolute difference: **every trade is virtual.** No order
is ever sent, no capital is committed, the Portfolio is never touched. This lets
the lab run any number of strategies against the live stream to measure what they
*would* have done, at zero risk.

The engine is a pure state machine: feed it observations, read its virtual
ledger. It has no collaborators in Execution/Risk/Portfolio — it cannot.
"""

from __future__ import annotations

from datetime import UTC, datetime

from hades.contexts.research.application import metrics as m
from hades.contexts.research.application.strategies import score_sample
from hades.contexts.research.domain.models import (
    ShadowStrategy,
    StrategyGenome,
    TradeRecord,
    VirtualTrade,
)
from hades.contexts.research.domain.ports import HistoricalSample


class ShadowStrategyRunner:
    """Owns one shadow strategy's virtual ledger and updates it per observation."""

    def __init__(self, shadow: ShadowStrategy) -> None:
        self._shadow = shadow
        self._genome: StrategyGenome = shadow.genome
        self._open: list[VirtualTrade] = list(shadow.open_trades)
        self._closed: list[TradeRecord] = []

    @property
    def shadow(self) -> ShadowStrategy:
        return self._shadow

    def observe(self, sample: HistoricalSample, *, at: datetime | None = None) -> ShadowStrategy:
        """Process one observation: maybe open a virtual trade, close matured ones.

        Returns the refreshed :class:`ShadowStrategy` snapshot. It never returns,
        implies, or triggers an order.
        """
        now = at or datetime.now(UTC)
        self._close_matured(sample, now)

        conviction = score_sample(self._genome, sample)
        mint = str(sample.get("mint", "")) or "unknown"
        already_open = any(t.mint == mint for t in self._open)
        if conviction >= self._genome.entry_score and not already_open:
            self._open.append(
                VirtualTrade(
                    mint=mint,
                    opened_at=now,
                    entry_score=round(conviction, 4),
                    size_usd=0.0,  # virtual: never any capital
                    reason=f"shadow:{self._genome.archetype.value}",
                )
            )

        return self._snapshot(now)

    def _close_matured(self, sample: HistoricalSample, now: datetime) -> None:
        """Close virtual trades whose forward return hit TP/SL (from the sample)."""
        fwd = float(sample.get("forward_return", 0.0))
        still_open: list[VirtualTrade] = []
        for trade in self._open:
            held = (now - trade.opened_at).total_seconds()
            hit_tp = fwd >= self._genome.take_profit
            hit_sl = fwd <= -self._genome.stop_loss
            expired = held >= self._genome.max_holding_seconds
            if hit_tp or hit_sl or expired:
                ret = (
                    self._genome.take_profit
                    if hit_tp
                    else -self._genome.stop_loss
                    if hit_sl
                    else fwd
                )
                self._closed.append(
                    TradeRecord(
                        opened_at=trade.opened_at,
                        closed_at=now,
                        ret=ret,
                        holding_seconds=max(held, 0.0),
                        mint=trade.mint,
                        reason=trade.reason,
                    )
                )
            else:
                still_open.append(trade)
        self._open = still_open

    def _snapshot(self, now: datetime) -> ShadowStrategy:
        self._shadow = self._shadow.model_copy(
            update={
                "open_trades": tuple(self._open),
                "metrics": m.compute(self._closed),
                "updated_at": now,
            }
        )
        return self._shadow
