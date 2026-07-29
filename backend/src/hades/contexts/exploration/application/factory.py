"""Assembles the immutable :class:`ExplorationConfig` from process settings.

One function, kept apart from the service so the policy can be constructed in a
test from a literal config without a settings object anywhere near it. The
translation is deliberately total — every knob in ``ExplorationSettings`` lands
in exactly one field here — so there is no setting an operator can change that
silently does nothing.
"""

from __future__ import annotations

from hades.contexts.exploration.domain.models import (
    EvidenceTarget,
    ExplorationBudget,
    ExplorationConfig,
)
from hades.shared_kernel.config import Settings


def exploration_config_from_settings(settings: Settings) -> ExplorationConfig:
    """Build the exploration policy from ``EXPLORATION_*`` configuration."""
    e = settings.exploration
    return ExplorationConfig(
        enabled=e.enabled,
        budget=ExplorationBudget(
            per_trade_usd=e.per_trade_usd,
            daily_usd=e.daily_budget_usd,
            weekly_usd=e.weekly_budget_usd,
            total_usd=e.total_budget_usd,
            max_trades_per_day=e.max_trades_per_day,
            max_trades_per_week=e.max_trades_per_week,
        ),
        target=EvidenceTarget(
            min_lessons=e.target_lessons,
            min_per_class=e.target_per_class,
            cohort_target=e.cohort_target,
        ),
        min_prob_roi_positive=e.min_prob_roi_positive,
        max_prob_roi_positive=e.max_prob_roi_positive,
        min_confidence=e.min_confidence,
        stop_loss_pct=e.stop_loss_pct,
        take_profit_pct=e.take_profit_pct,
    )


__all__ = ["exploration_config_from_settings"]
