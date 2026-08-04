"""EnsembleBuilder - fuses many strategy signals into one, never by simple vote.

The spec is explicit: *never* count votes. Each signal contributes a *signed,
weighted* pull on the decision - ``weight - confidence - internal_score -
direction`` - where the weight is the dynamic weight (regime fit, recent quality,
AI confidence, ...). BUY pulls positive, SELL/EXIT pull negative, IGNORE is silent.
The net conviction is normalised into [-1, 1]; the decision is the side it lands
on past a small deadband; the ensemble confidence blends conviction magnitude with
the agreement among the participating strategies.

Shadow-stage strategies are recorded as contributions but excluded from the
production decision - their column shows what they *would* have said.
"""

from __future__ import annotations

from hades.contexts.strategy.domain.models import (
    EnsembleContribution,
    EnsembleSignal,
    MarketContext,
    SignalType,
    StrategySignal,
    StrategyWeight,
)


class EnsembleBuilder:
    """Combines weighted strategy signals into a single :class:`EnsembleSignal`."""

    def __init__(self, *, decision_deadband: float = 0.12) -> None:
        # Net conviction must exceed this to become an actionable decision.
        self._deadband = decision_deadband

    def build(
        self,
        context: MarketContext,
        signals: list[StrategySignal],
        weights: dict[str, StrategyWeight],
        *,
        correlation_id: str | None = None,
    ) -> EnsembleSignal:
        contributions: list[EnsembleContribution] = []
        weighted_sum = 0.0  # signed
        total_weight = 0.0  # magnitude of participating production weight
        conf_weighted = 0.0
        buy_pull = 0.0
        sell_pull = 0.0

        for signal in signals:
            w = weights.get(signal.strategy)
            weight = w.weight if w else 1.0
            direction = signal.signal_type.direction
            magnitude = weight * signal.confidence * signal.internal_score
            contribution = magnitude * direction
            contributions.append(
                EnsembleContribution(
                    strategy=signal.strategy,
                    signal_type=signal.signal_type,
                    weight=round(weight, 5),
                    confidence=signal.confidence,
                    internal_score=signal.internal_score,
                    contribution=round(contribution, 5),
                    shadow=signal.shadow,
                )
            )
            # Shadow strategies never move the production decision.
            if signal.shadow or direction == 0:
                continue
            weighted_sum += contribution
            total_weight += weight
            conf_weighted += weight * signal.confidence
            if direction > 0:
                buy_pull += magnitude
            else:
                sell_pull += magnitude

        participating = sum(
            1 for c in contributions if not c.shadow and c.signal_type.is_actionable
        )
        score = (weighted_sum / total_weight) if total_weight > 0 else 0.0
        decision = self._decide(score)
        agreement = self._agreement(buy_pull, sell_pull, decision)
        base_conf = (conf_weighted / total_weight) if total_weight > 0 else 0.0
        confidence = _clamp01(0.6 * abs(score) + 0.4 * base_conf * agreement)

        return EnsembleSignal(
            token=context.token,
            regime=context.regime,
            decision=decision,
            score=round(score, 5),
            confidence=round(confidence, 4),
            agreement=round(agreement, 4),
            participating=participating,
            reasoning=self._reason(decision, contributions, score, agreement),
            contributions=tuple(contributions),
            ai_confidence=context.ai_confidence,
            correlation_id=correlation_id,
        )

    def _decide(self, score: float) -> SignalType:
        if score >= self._deadband:
            return SignalType.BUY
        if score <= -self._deadband:
            return SignalType.EXIT
        return SignalType.IGNORE

    @staticmethod
    def _agreement(buy_pull: float, sell_pull: float, decision: SignalType) -> float:
        total = buy_pull + sell_pull
        if total <= 0:
            return 0.0
        if decision is SignalType.BUY:
            return buy_pull / total
        if decision in (SignalType.SELL, SignalType.EXIT):
            return sell_pull / total
        return 0.0

    @staticmethod
    def _reason(
        decision: SignalType,
        contributions: list[EnsembleContribution],
        score: float,
        agreement: float,
    ) -> str:
        actionable = [c for c in contributions if not c.shadow and c.signal_type.is_actionable]
        top = sorted(actionable, key=lambda c: abs(c.contribution), reverse=True)[:3]
        leaders = ", ".join(f"{c.strategy}({c.contribution:+.2f})" for c in top)
        if decision is SignalType.IGNORE:
            return f"no consensus (net {score:+.2f}); leaders: {leaders or 'none'}"
        return f"{decision.value} net {score:+.2f}, agreement {agreement:.0%}; led by {leaders}"


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))
