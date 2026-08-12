"""Security Engine Prometheus metrics — one declaration site for the context.

Everything the spec asks the engine to surface: tokens approved/rejected, the
reasons behind rejections, the specific risks found, wallet clusters detected,
per-analysis latency, and running averages for the developer and security
scores. Typed accessors over the shared registry.
"""

from __future__ import annotations

from hades.shared_kernel.observability import MetricsRegistry


class SecurityMetrics:
    """Typed accessors over the shared metrics registry for the Security Engine."""

    def __init__(self, metrics: MetricsRegistry) -> None:
        self.analyzed = metrics.counter(
            "hades_security_tokens_analyzed_total",
            "Tokens fully analysed by the Security Engine",
        )
        self.approved = metrics.counter(
            "hades_security_tokens_approved_total",
            "Tokens that cleared the Security Engine",
        )
        self.rejected = metrics.counter(
            "hades_security_tokens_rejected_total",
            "Tokens rejected by the Security Engine, by primary reason",
            ("reason",),
        )
        self.risks = metrics.counter(
            "hades_security_risk_flags_total",
            "Individual risk flags raised, by code and severity",
            ("code", "severity"),
        )
        self.clusters = metrics.counter(
            "hades_security_clusters_detected_total",
            "Wallet clusters detected across analysed tokens",
        )
        self.vetoes = metrics.counter(
            "hades_security_vetoes_total",
            "Tokens hard-vetoed by a critical flag",
        )
        self.errors = metrics.counter(
            "hades_security_errors_total",
            "Unhandled errors while analysing a token",
        )
        self.analysis_seconds = metrics.histogram(
            "hades_security_analysis_seconds",
            "Wall-clock time to fully analyse one token",
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
        )
        # The split that mattered and did not exist. `analysis_seconds` covers the
        # engine only, so the assembler's I/O -- which turned out to be the whole
        # cost -- was invisible: a session spent arguing from code structure about
        # which half was slow, and could not show afterwards whether making it
        # concurrent had helped. Two histograms answer that directly, and they are
        # deliberately permanent rather than a temporary probe, because the
        # question recurs every time the pipeline falls behind.
        self.assemble_seconds = metrics.histogram(
            "hades_security_assemble_seconds",
            "Time spent gathering a token's analysis context (I/O: RPC, stored facts, "
            "honeypot probe, developer reputation, clustering)",
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
        )
        self.assemble_step_seconds = metrics.histogram(
            "hades_security_assemble_step_seconds",
            "Time spent on one assembler source, by step",
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0),
            labelnames=("step",),
        )
        self.analyzers_seconds = metrics.histogram(
            "hades_security_analyzers_seconds",
            "Time spent running the ten analyzers (pure computation, no I/O)",
            buckets=(0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0),
        )
        self.security_score = metrics.gauge(
            "hades_security_score_last",
            "Most recent final security score",
        )
        self.developer_score = metrics.gauge(
            "hades_security_developer_score_last",
            "Most recent developer sub-score",
        )
