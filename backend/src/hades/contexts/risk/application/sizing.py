"""Position Sizing Engine - dynamic, conviction-weighted trade sizing.

Sizing is *never* a fixed lot. It is derived, per trade, from how good the
opportunity is and how much room the book has. The engine blends the AI
Committee's probability + confidence with the upstream security, wallet,
liquidity and regime signals into a single ``conviction`` in [0, 1], turns that
into an amount of capital to *risk* (a fraction of equity), and converts the
risk into a notional via the stop distance:

    risk_usd  = equity x risk_per_trade% x conviction x kill_switch_factor
    notional  = risk_usd / stop_loss_fraction        (bounded by caps + cash)

The result reproduces the intent in the spec - a stellar setup gets a large
size, a marginal one a small size, and anything below the minimum is not traded
at all. It is pure: no I/O, fully deterministic, unit-testable. It only *sizes*;
it never decides to trade (the policy chain does) and never executes.
"""

from __future__ import annotations

from hades.contexts.common.domain.value_objects import Percentage
from hades.contexts.risk.domain.models import (
    ExplorationGrant,
    PositionSizing,
    RiskCandidate,
    SizingConfig,
    SizingDecision,
    to_money,
)

# Weight each conviction driver contributes to the blended conviction. They sum
# to 1.0 so conviction stays in [0, 1]. Probability dominates; the rest modulate.
_WEIGHTS = {
    "probability": 0.40,
    "edge": 0.20,
    "security": 0.15,
    "wallet": 0.10,
    "liquidity": 0.10,
    "regime": 0.05,
}

# Regime multipliers - favourable regimes keep full conviction, hostile ones cut
# it. Sizing is *reduced* in doubt, never amplified beyond the base.
_REGIME_FACTOR = {
    "bull": 1.0,
    "fomo": 1.0,
    "sideways": 0.85,
    "highly_volatile": 0.7,
    "low_liquidity": 0.6,
    "bear": 0.55,
    "panic": 0.4,
    "unknown": 0.8,
}


class PositionSizingEngine:
    """Turns a candidate + capital + kill-switch factor into a sized envelope."""

    def __init__(self, config: SizingConfig) -> None:
        self._cfg = config

    def size(
        self,
        candidate: RiskCandidate,
        *,
        equity_usd: float,
        available_usd: float,
        kill_switch_factor: float = 1.0,
    ) -> SizingDecision:
        """Compute the sizing for one candidate. Never raises; degrades to a
        not-approved decision when there is no room or no conviction."""
        conviction, factors = self._conviction(candidate)
        conviction *= max(0.0, min(1.0, kill_switch_factor))
        factors["kill_switch"] = round(kill_switch_factor, 4)
        factors["conviction"] = round(conviction, 4)

        stop_fraction = max(0.01, self._cfg.stop_loss_pct / 100.0)
        risk_usd = equity_usd * (self._cfg.risk_per_trade_pct / 100.0) * conviction
        raw_notional = risk_usd / stop_fraction

        # Bound by the hard per-trade cap and the deployable cash.
        notional = min(raw_notional, self._cfg.max_position_size_usd, max(0.0, available_usd))
        notional = round(notional, 6)

        if notional < self._cfg.min_position_size_usd or conviction <= 0.0:
            return SizingDecision(
                approved=False,
                conviction=round(conviction, 4),
                risk_amount_usd=round(risk_usd, 6),
                factors=factors,
                detail=(
                    "below minimum position size"
                    if conviction > 0
                    else "no conviction after adjustments"
                ),
            )

        realised_risk = round(notional * stop_fraction, 6)
        return SizingDecision(
            approved=True,
            sizing=self._envelope(notional),
            conviction=round(conviction, 4),
            risk_amount_usd=realised_risk,
            factors=factors,
            detail=f"sized to ${notional:.4f} at conviction {conviction:.2f}",
        )

    def explore(self, grant: ExplorationGrant, *, available_usd: float) -> SizingDecision:
        """Size an *exploration* trade: fixed amount, tight stop, no conviction.

        A separate method rather than a branch inside :meth:`size`, because the
        two are different ideas that happen to produce the same shape. Normal
        sizing asks "how much does this opportunity deserve?"; exploration
        answers "how much does one sample cost?", and the answer was fixed in
        configuration before the candidate existed. Blending conviction in here
        would reintroduce, at the sizing step, the very belief the programme
        exists to test — and would do it invisibly, since the result is still
        just a number of dollars.

        The cash check remains. An exploration trade may be tiny and it may waive
        a conviction gate, but it may not spend money the book does not have:
        ``available_usd`` is already net of the mandatory liquidity reserve, so
        respecting it here means the reserve cannot be breached to buy evidence.
        """
        notional = round(min(grant.notional_usd, max(0.0, available_usd)), 6)
        stop_fraction = max(0.01, grant.stop_loss_pct / 100.0)
        factors = {
            "exploration": 1.0,
            "conviction": 0.0,
            "requested_usd": round(grant.notional_usd, 6),
            "available_usd": round(max(0.0, available_usd), 6),
        }
        if notional < grant.notional_usd or notional <= 0.0:
            return SizingDecision(
                approved=False,
                conviction=0.0,
                factors=factors,
                detail=(
                    f"exploration needs ${grant.notional_usd:.4f}, "
                    f"${max(0.0, available_usd):.4f} deployable after reserve"
                ),
            )
        return SizingDecision(
            approved=True,
            sizing=PositionSizing(
                notional=to_money(notional),
                take_profit=Percentage(value=grant.take_profit_pct),
                stop_loss=Percentage(value=grant.stop_loss_pct),
                # No trailing stop on an exploration trade. A trailing exit makes
                # the realised return depend on the path *and* on the exit rule,
                # which muddies the one thing the trade was bought for: a clean
                # label for what this kind of candidate does.
                trailing_enabled=False,
                trailing_activation=Percentage(value=0.0),
                trailing_distance=Percentage(value=0.0),
            ),
            conviction=0.0,
            risk_amount_usd=round(notional * stop_fraction, 6),
            factors=factors,
            detail=f"exploration sample sized to a fixed ${notional:.4f}",
        )

    # -- internals ------------------------------------------------------------

    def _conviction(self, c: RiskCandidate) -> tuple[float, dict[str, float]]:
        """Blend the signals into a single conviction with an audit of parts."""
        edge = max(0.0, min(1.0, (c.expected_edge + 1.0) / 2.0))
        regime = _REGIME_FACTOR.get(c.regime, 0.8)
        parts = {
            "probability": c.prob_roi_positive,
            "edge": edge,
            "security": max(0.0, min(1.0, c.security_score / 100.0)),
            "wallet": max(0.0, min(1.0, (100.0 - c.wallet_risk_score) / 100.0)),
            "liquidity": max(0.0, min(1.0, c.liquidity_score / 100.0)),
            "regime": regime,
        }
        blended = sum(_WEIGHTS[k] * v for k, v in parts.items())
        # Confidence gates conviction: a low-confidence call is sized down, never
        # up. At confidence 0 conviction is halved; at 1 it is untouched.
        blended *= 0.5 + 0.5 * c.confidence
        factors = {k: round(v, 4) for k, v in parts.items()}
        factors["confidence"] = round(c.confidence, 4)
        return max(0.0, min(1.0, blended)), factors

    def _envelope(self, notional_usd: float) -> PositionSizing:
        return PositionSizing(
            notional=to_money(notional_usd),
            take_profit=Percentage(value=self._cfg.take_profit_pct),
            stop_loss=Percentage(value=self._cfg.stop_loss_pct),
            trailing_enabled=self._cfg.trailing_enabled,
            trailing_activation=Percentage(value=self._cfg.trailing_activation_pct),
            trailing_distance=Percentage(value=self._cfg.trailing_distance_pct),
        )
