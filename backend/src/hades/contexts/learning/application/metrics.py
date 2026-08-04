"""AI Committee Prometheus metrics — one declaration site for the context.

Surfaces what the spec asks the brain to expose: committee runs and their
latency, per-member abstentions, the last fused probabilities/confidence,
models registered/promoted, training runs, and detected drift. Typed accessors
over the shared registry.
"""

from __future__ import annotations

from hades.shared_kernel.observability import MetricsRegistry


class LearningMetrics:
    """Typed accessors over the shared metrics registry for the AI Committee."""

    def __init__(self, metrics: MetricsRegistry) -> None:
        self.committee_runs = metrics.counter(
            "hades_committee_runs_total",
            "Committee evaluations completed for a token",
        )
        self.opinions = metrics.counter(
            "hades_committee_opinions_total",
            "Specialist opinions emitted, by member",
            ("member",),
        )
        self.abstentions = metrics.counter(
            "hades_committee_abstentions_total",
            "Specialist abstentions (coverage too low), by member",
            ("member",),
        )
        self.errors = metrics.counter(
            "hades_committee_errors_total",
            "Unhandled errors while running the committee",
        )
        self.enrichments = metrics.counter(
            "hades_committee_enrichments_total",
            "Candidates enriched from the Knowledge Engine, by evidence outcome",
            ("evidence",),
        )
        self.enrichment_seconds = metrics.histogram(
            "hades_committee_enrichment_seconds",
            "Wall-clock time to enrich one candidate from permanent memory",
            buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
        )
        self.last_prior_log_odds = metrics.gauge(
            "hades_committee_last_prior_log_odds",
            "Most recent historical prior applied to the fused probabilities",
        )
        self.last_enrichment_samples = metrics.gauge(
            "hades_committee_last_enrichment_samples",
            "Comparable historical samples behind the most recent enrichment",
        )
        self.models_registered = metrics.counter(
            "hades_models_registered_total",
            "Models appended to the registry",
        )
        self.models_promoted = metrics.counter(
            "hades_models_promoted_total",
            "Models promoted to active (human/policy gated)",
        )
        self.models_rejected = metrics.counter(
            "hades_models_rejected_total",
            "Candidate models rejected by validation",
        )
        self.training_runs = metrics.counter(
            "hades_training_runs_total",
            "Training runs executed by the training engine",
        )
        self.drift_alerts = metrics.counter(
            "hades_model_drift_alerts_total",
            "Model-drift alerts raised, by kind",
            ("kind",),
        )
        self.run_seconds = metrics.histogram(
            "hades_committee_run_seconds",
            "Wall-clock time for one committee evaluation",
            buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
        )
        self.last_roi_probability = metrics.gauge(
            "hades_committee_last_roi_probability",
            "Most recent P(ROI positive) fused by the meta-model",
        )
        self.last_confidence = metrics.gauge(
            "hades_committee_last_confidence",
            "Most recent committee confidence",
        )
