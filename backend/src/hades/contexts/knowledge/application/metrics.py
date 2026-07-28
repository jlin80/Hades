"""Knowledge Prometheus metrics — one declaration site for the context.

The gauges are the ones worth alerting on. ``open_decisions`` climbing without
``lessons`` following means decisions are being taken and never settled — the
learning loop breaking again, visible within minutes instead of after a session
of archaeology. ``positive_rate`` sitting at exactly 0.0 or 1.0 means the memory
holds a single class and no model can be validated against it, which is the
condition that kept this platform in cold start.
"""

from __future__ import annotations

from hades.shared_kernel.observability import MetricsRegistry


class KnowledgeMetrics:
    """Typed accessors over the shared metrics registry for Knowledge."""

    def __init__(self, metrics: MetricsRegistry) -> None:
        self.recorded = metrics.counter(
            "hades_knowledge_recorded_total",
            "Facts accepted into permanent memory, by provenance",
            ("source", "kind"),
        )
        self.rejected = metrics.counter(
            "hades_knowledge_rejected_total",
            "Records refused at the ingestion boundary, by provenance",
            ("source",),
        )
        self.store_errors = metrics.counter(
            "hades_knowledge_store_errors_total",
            "Failures writing to the knowledge store",
        )
        self.decisions_recorded = metrics.counter(
            "hades_knowledge_decisions_total",
            "Decisions frozen with their evidence, awaiting an outcome",
        )
        self.lessons_learned = metrics.counter(
            "hades_knowledge_lessons_total",
            "Decision-outcome pairs completed (ground-truth training samples)",
        )
        self.orphan_outcomes = metrics.counter(
            "hades_knowledge_orphan_outcomes_total",
            "Outcomes arriving with no matching decision in the journal",
        )
        self.total = metrics.gauge(
            "hades_knowledge_observations",
            "Observations held in permanent memory",
        )
        self.open_decisions = metrics.gauge(
            "hades_knowledge_open_decisions",
            "Decisions recorded but not yet settled",
        )
        # Not ``hades_knowledge_lessons``: prometheus_client strips the ``_total``
        # suffix when naming a counter's timeseries, so that gauge would collide
        # with ``hades_knowledge_lessons_total`` above and the registry would
        # refuse both.
        self.lessons = metrics.gauge(
            "hades_knowledge_lessons_stored",
            "Completed decision-outcome pairs held",
        )
        self.positive_rate = metrics.gauge(
            "hades_knowledge_positive_rate",
            "Share of lessons with a positive realised return (0 or 1 means untrainable)",
        )


__all__ = ["KnowledgeMetrics"]
