"""The risk policy chain - the composable rules the Risk Manager runs.

Each policy owns exactly one rule and returns a :class:`PolicyOutcome`. The
manager runs the whole chain; any failure vetoes the trade and every failure
names a distinct :class:`RiskRejectReason`, so a rejection is always traceable
to the rule that caused it. Rules are pure - they read the pre-assembled
candidate and portfolio state and do no I/O - which makes them trivially
unit-testable and lets new rules be added without touching existing ones.

The chain splits into two phases:

* **Quality** rules (``proposed_notional`` ignored) - is the opportunity itself
  good enough? Probability, confidence, security, developer, wallet, liquidity.
* **Allocation** rules (use the intended size) - does the *book* have room?
  Position count, capital, exposure, correlation, risk budget, drawdown, rate.
"""

from __future__ import annotations

from hades.contexts.risk.application.correlation import CorrelationEngine
from hades.contexts.risk.application.drawdown import DrawdownEngine
from hades.contexts.risk.application.exposure import ExposureEngine
from hades.contexts.risk.application.risk_budget import RiskBudgetEngine
from hades.contexts.risk.domain.models import (
    DrawdownLimits,
    ExposureLimits,
    PolicyOutcome,
    PortfolioRiskState,
    RateLimits,
    RiskBudget,
    RiskCandidate,
    RiskRejectReason,
    SizingConfig,
)


def _ok(policy: str) -> PolicyOutcome:
    return PolicyOutcome(policy=policy, passed=True)


def _veto(policy: str, reason: RiskRejectReason, detail: str) -> PolicyOutcome:
    return PolicyOutcome(policy=policy, passed=False, reason=reason, detail=detail)


# =============================================================================
# Quality policies (evaluated before sizing)
# =============================================================================


class MinProbabilityPolicy:
    name = "min_probability"

    def __init__(self, config: SizingConfig) -> None:
        self._cfg = config

    def evaluate(
        self, candidate: RiskCandidate, state: PortfolioRiskState, proposed_notional_usd: float
    ) -> PolicyOutcome:
        if candidate.prob_roi_positive < self._cfg.min_prob_roi_positive:
            return _veto(
                self.name,
                RiskRejectReason.LOW_PROBABILITY,
                f"P(ROI+) {candidate.prob_roi_positive:.2f} "
                f"< {self._cfg.min_prob_roi_positive:.2f}",
            )
        return _ok(self.name)


class MinConfidencePolicy:
    name = "min_confidence"

    def __init__(self, config: SizingConfig) -> None:
        self._cfg = config

    def evaluate(
        self, candidate: RiskCandidate, state: PortfolioRiskState, proposed_notional_usd: float
    ) -> PolicyOutcome:
        if candidate.confidence < self._cfg.min_confidence:
            return _veto(
                self.name,
                RiskRejectReason.LOW_CONFIDENCE,
                f"confidence {candidate.confidence:.2f} < {self._cfg.min_confidence:.2f}",
            )
        return _ok(self.name)


class EnsembleConsensusPolicy:
    """The Strategy Engine's fused opinion, as a veto on entries.

    For most of this platform's life the Strategy Engine computed into nothing:
    fifteen strategies, a weighted ensemble and a dynamic weight engine all
    published ``EnsembleSignalGenerated``, and no subscriber existed. The audit
    catalogued it as the largest piece of dead machinery in the codebase.

    This is where it starts to matter, and the direction is deliberate: the
    ensemble may **block** an entry, never create one. The Risk Manager remains
    the sole authoriser; adding a second voice that could *approve* would end
    that, whereas a second voice that can only refuse is strictly conservative —
    with this policy on, the set of approved trades can only shrink.

    Two things it refuses to do:

    * **Treat silence as dissent.** ``ensemble_participating == 0`` means no
      strategy has spoken for this token. At cold start that is the normal case,
      and reading it as a veto would silently halt the platform while looking
      like ordinary caution.
    * **Outrank safety.** This lives in the CONVICTION tier, so an exploration
      grant may waive it. That is intentional: exploration exists to sample
      cohorts the platform has no evidence about, and strategies with no history
      have no opinion worth letting block that. It could never waive a SAFETY
      rule, and this is not one.
    """

    name = "ensemble_consensus"

    def __init__(self, min_score: float) -> None:
        self._min_score = min_score

    def evaluate(
        self, candidate: RiskCandidate, state: PortfolioRiskState, proposed_notional_usd: float
    ) -> PolicyOutcome:
        if candidate.ensemble_participating <= 0:
            return _ok(self.name)
        if candidate.ensemble_decision != "buy_signal":
            return _veto(
                self.name,
                RiskRejectReason.ENSEMBLE_DISAGREES,
                f"strategy ensemble says {candidate.ensemble_decision} "
                f"({candidate.ensemble_participating} strategies)",
            )
        if candidate.ensemble_score < self._min_score:
            return _veto(
                self.name,
                RiskRejectReason.ENSEMBLE_DISAGREES,
                f"ensemble conviction {candidate.ensemble_score:.2f} < {self._min_score:.2f}",
            )
        return _ok(self.name)


class SecurityPolicy:
    name = "security"

    def __init__(self, config: SizingConfig) -> None:
        self._cfg = config

    def evaluate(
        self, candidate: RiskCandidate, state: PortfolioRiskState, proposed_notional_usd: float
    ) -> PolicyOutcome:
        if not candidate.security_approved:
            return _veto(
                self.name, RiskRejectReason.SECURITY_REJECTED, "security engine rejected the token"
            )
        if candidate.security_score < self._cfg.min_security_score:
            return _veto(
                self.name,
                RiskRejectReason.LOW_SECURITY_SCORE,
                f"security {candidate.security_score:.0f} < {self._cfg.min_security_score:.0f}",
            )
        return _ok(self.name)


class DeveloperPolicy:
    name = "developer"

    def __init__(self, min_developer_score: float = 40.0) -> None:
        self._min = min_developer_score

    def evaluate(
        self, candidate: RiskCandidate, state: PortfolioRiskState, proposed_notional_usd: float
    ) -> PolicyOutcome:
        if candidate.developer_score < self._min:
            return _veto(
                self.name,
                RiskRejectReason.DEVELOPER_RISKY,
                f"developer score {candidate.developer_score:.0f} < {self._min:.0f}",
            )
        return _ok(self.name)


class WalletPolicy:
    name = "wallet"

    def __init__(self, max_wallet_risk: float = 70.0) -> None:
        self._max = max_wallet_risk

    def evaluate(
        self, candidate: RiskCandidate, state: PortfolioRiskState, proposed_notional_usd: float
    ) -> PolicyOutcome:
        if candidate.wallet_risk_score > self._max:
            return _veto(
                self.name,
                RiskRejectReason.WALLET_SUSPICIOUS,
                f"wallet risk {candidate.wallet_risk_score:.0f} > {self._max:.0f}",
            )
        return _ok(self.name)


class LiquidityPolicy:
    name = "liquidity"

    def __init__(self, min_liquidity_usd: float = 5_000.0) -> None:
        self._min = min_liquidity_usd

    def evaluate(
        self, candidate: RiskCandidate, state: PortfolioRiskState, proposed_notional_usd: float
    ) -> PolicyOutcome:
        # A liquidity of 0 means "unknown" (no fact available) - security already
        # gated liquidity upstream, so unknown does not block here; only a
        # positively-known thin pool is rejected.
        if 0.0 < candidate.liquidity_usd < self._min:
            return _veto(
                self.name,
                RiskRejectReason.LIQUIDITY_INSUFFICIENT,
                f"liquidity ${candidate.liquidity_usd:,.0f} < ${self._min:,.0f}",
            )
        return _ok(self.name)


# =============================================================================
# Allocation policies (evaluated with the intended size)
# =============================================================================


class MaxPositionsPolicy:
    name = "max_positions"

    def __init__(self, limits: RateLimits) -> None:
        self._limits = limits

    def evaluate(
        self, candidate: RiskCandidate, state: PortfolioRiskState, proposed_notional_usd: float
    ) -> PolicyOutcome:
        if state.open_count >= self._limits.max_concurrent_positions:
            return _veto(
                self.name,
                RiskRejectReason.MAX_POSITIONS,
                f"{state.open_count} open >= {self._limits.max_concurrent_positions} max",
            )
        return _ok(self.name)


class CapitalPolicy:
    name = "capital"

    def evaluate(
        self, candidate: RiskCandidate, state: PortfolioRiskState, proposed_notional_usd: float
    ) -> PolicyOutcome:
        available = state.capital.available_usd
        if proposed_notional_usd > available:
            return _veto(
                self.name,
                RiskRejectReason.INSUFFICIENT_CAPITAL,
                f"need ${proposed_notional_usd:,.2f}, available ${available:,.2f} after reserve",
            )
        return _ok(self.name)


class ExposurePolicy:
    name = "exposure"

    def __init__(self, limits: ExposureLimits) -> None:
        self._limits = limits
        self._engine = ExposureEngine(limits)

    def evaluate(
        self, candidate: RiskCandidate, state: PortfolioRiskState, proposed_notional_usd: float
    ) -> PolicyOutcome:
        total_after = self._engine.total_exposure_pct(state) + (
            proposed_notional_usd / state.capital.equity_usd * 100.0
            if state.capital.equity_usd > 0
            else 0.0
        )
        if total_after > self._limits.max_portfolio_pct:
            return _veto(
                self.name,
                RiskRejectReason.PORTFOLIO_EXPOSURE,
                f"portfolio exposure {total_after:.1f}% > {self._limits.max_portfolio_pct:.1f}%",
            )
        breach = self._engine.check(candidate, state, proposed_notional_usd)
        if breach is not None:
            reason = {
                "token": RiskRejectReason.TOKEN_EXPOSURE,
                "developer": RiskRejectReason.DEVELOPER_EXPOSURE,
                "cluster": RiskRejectReason.CLUSTER_EXPOSURE,
                "strategy": RiskRejectReason.STRATEGY_EXPOSURE,
                "narrative": RiskRejectReason.NARRATIVE_EXPOSURE,
                "regime": RiskRejectReason.REGIME_EXPOSURE,
            }.get(breach.axis.value, RiskRejectReason.PORTFOLIO_EXPOSURE)
            return _veto(
                self.name,
                reason,
                f"{breach.axis.value} '{breach.key}' {breach.exposure_pct:.1f}% "
                f"> {breach.limit_pct:.1f}%",
            )
        return _ok(self.name)


class CorrelationPolicy:
    name = "correlation"

    def __init__(self, limits: RateLimits) -> None:
        self._engine = CorrelationEngine(limits.max_correlated_positions)

    def evaluate(
        self, candidate: RiskCandidate, state: PortfolioRiskState, proposed_notional_usd: float
    ) -> PolicyOutcome:
        if self._engine.is_overcorrelated(candidate, state):
            count = self._engine.correlation_count(candidate, state)
            return _veto(
                self.name,
                RiskRejectReason.CORRELATED_POSITIONS,
                f"{count} correlated positions already open (developer/cluster/narrative)",
            )
        return _ok(self.name)


class RiskBudgetPolicy:
    name = "risk_budget"

    def __init__(self, budget: RiskBudget) -> None:
        self._engine = RiskBudgetEngine(budget)

    def evaluate(
        self, candidate: RiskCandidate, state: PortfolioRiskState, proposed_notional_usd: float
    ) -> PolicyOutcome:
        exceeds, projected, budget = self._engine.would_exceed(
            state, candidate.strategy, proposed_notional_usd
        )
        if exceeds:
            return _veto(
                self.name,
                RiskRejectReason.RISK_BUDGET_EXHAUSTED,
                f"strategy '{candidate.strategy}' {projected:.1f}% > budget {budget:.1f}%",
            )
        return _ok(self.name)


class DrawdownPolicy:
    name = "drawdown"

    def __init__(self, limits: DrawdownLimits) -> None:
        self._limits = limits
        self._engine = DrawdownEngine(limits)

    def evaluate(
        self, candidate: RiskCandidate, state: PortfolioRiskState, proposed_notional_usd: float
    ) -> PolicyOutcome:
        if self._engine.stop_limit_breached(state.drawdown):
            return _veto(
                self.name,
                RiskRejectReason.DAILY_STOP_LIMIT,
                f"{state.drawdown.daily_stop_losses} stop-losses today "
                f">= {self._limits.daily_max_stop_losses}",
            )
        breach = self._engine.evaluate(state.drawdown)
        if breach is not None:
            reason = {
                "daily": RiskRejectReason.DAILY_LOSS_LIMIT,
                "daily_usd": RiskRejectReason.DAILY_LOSS_LIMIT,
                "weekly": RiskRejectReason.WEEKLY_LOSS_LIMIT,
                "monthly": RiskRejectReason.MONTHLY_LOSS_LIMIT,
            }.get(breach.window, RiskRejectReason.DAILY_LOSS_LIMIT)
            return _veto(
                self.name,
                reason,
                f"{breach.window} loss {breach.loss_pct:.1f} >= limit {breach.limit_pct:.1f}",
            )
        return _ok(self.name)


class TradeRatePolicy:
    name = "trade_rate"

    def __init__(self, limits: RateLimits) -> None:
        self._limits = limits

    def evaluate(
        self, candidate: RiskCandidate, state: PortfolioRiskState, proposed_notional_usd: float
    ) -> PolicyOutcome:
        if state.trades_last_hour >= self._limits.max_trades_per_hour:
            return _veto(
                self.name,
                RiskRejectReason.TRADE_RATE_LIMIT,
                f"{state.trades_last_hour} trades this hour >= {self._limits.max_trades_per_hour}",
            )
        per_strategy = state.trades_last_hour_by_strategy.get(candidate.strategy, 0)
        if per_strategy >= self._limits.max_trades_per_hour_per_strategy:
            return _veto(
                self.name,
                RiskRejectReason.TRADE_RATE_LIMIT,
                f"strategy '{candidate.strategy}' {per_strategy} trades this hour "
                f">= {self._limits.max_trades_per_hour_per_strategy}",
            )
        return _ok(self.name)
