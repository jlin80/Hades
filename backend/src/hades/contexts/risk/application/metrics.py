"""Risk Manager Prometheus metrics - one declaration site for the context.

Surfaces what the guardian must expose: approvals and rejections (by reason),
the live Kill Switch level and Circuit Breaker posture, emergency mode, and the
latency of a risk review. Typed accessors over the shared registry.
"""

from __future__ import annotations

from hades.shared_kernel.observability import MetricsRegistry


class RiskMetrics:
    """Typed accessors over the shared metrics registry for the Risk Manager."""

    def __init__(self, metrics: MetricsRegistry) -> None:
        self.approvals = metrics.counter(
            "hades_risk_trade_approvals_total",
            "Trades approved by the Risk Manager",
        )
        self.rejections = metrics.counter(
            "hades_risk_trade_rejections_total",
            "Trades rejected by the Risk Manager, by reason",
            ("reason",),
        )
        # Counted apart from ``approvals`` because the two must never be summed
        # together when judging the platform's results: an exploration trade was
        # taken to generate evidence, not because the platform believed in it,
        # and folding it into the headline approval count would let a programme
        # of deliberate small losses read as a strategy performing badly.
        self.exploration_approvals = metrics.counter(
            "hades_risk_exploration_approvals_total",
            "Approved trades taken under the exploration programme",
        )
        self.errors = metrics.counter(
            "hades_risk_errors_total",
            "Unhandled errors while evaluating risk",
        )
        self.kill_switch_changes = metrics.counter(
            "hades_risk_kill_switch_changes_total",
            "Kill Switch level transitions",
        )
        self.circuit_breaker_trips = metrics.counter(
            "hades_risk_circuit_breaker_trips_total",
            "Circuit Breaker trips, by reason",
            ("reason",),
        )
        self.review_seconds = metrics.histogram(
            "hades_risk_review_seconds",
            "Wall-clock time for one risk review",
            buckets=(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25),
        )
        self.kill_switch_level = metrics.gauge(
            "hades_risk_kill_switch_level",
            "Current Kill Switch level (0-5)",
        )
        self.circuit_breaker_open = metrics.gauge(
            "hades_risk_circuit_breaker_open",
            "1 when the Circuit Breaker is open, else 0",
        )
        self.emergency_mode = metrics.gauge(
            "hades_risk_emergency_mode",
            "1 when Emergency Mode is active, else 0",
        )
        self.portfolio_exposure_pct = metrics.gauge(
            "hades_risk_portfolio_exposure_pct",
            "Current portfolio exposure as a percentage of equity",
        )
