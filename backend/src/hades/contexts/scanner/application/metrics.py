"""Scanner Prometheus metrics — one declaration site for the whole context.

Everything the spec asks the Scanner to "register constantly" lives here:
tokens discovered/active, per-stage timing, errors, timeouts, DEX availability,
features throughput and the processing queue. The RPC latency gauges are owned by
the :class:`~hades.shared_kernel.solana.rpc_manager.RpcManager` itself.
"""

from __future__ import annotations

from hades.shared_kernel.observability import MetricsRegistry


class ScannerMetrics:
    """Typed accessors over the shared metrics registry for the Scanner."""

    def __init__(self, metrics: MetricsRegistry) -> None:
        self.discovered = metrics.counter(
            "hades_scanner_tokens_discovered_total",
            "Tokens that passed coarse gating and entered the pipeline",
            ("source",),
        )
        self.rejected = metrics.counter(
            "hades_scanner_candidates_rejected_total",
            "Raw candidates dropped by dedup/gating",
            ("reason",),
        )
        self.processed = metrics.counter(
            "hades_scanner_tokens_processed_total",
            "Tokens fully processed by the acquisition pipeline",
            ("outcome",),
        )
        self.stage_errors = metrics.counter(
            "hades_scanner_stage_errors_total",
            "Errors by pipeline stage",
            ("stage",),
        )
        self.stage_timeouts = metrics.counter(
            "hades_scanner_stage_timeouts_total",
            "Timeouts by pipeline stage",
            ("stage",),
        )
        self.anomalies = metrics.counter(
            "hades_scanner_anomalies_total",
            "Data-quality anomalies detected",
            ("kind",),
        )
        self.backpressure_drops = metrics.counter(
            "hades_scanner_backpressure_drops_total",
            "Candidates dropped because the processing queue was full",
        )
        self.features_computed = metrics.counter(
            "hades_scanner_features_computed_total",
            "Individual feature values computed",
        )
        self.analysis_seconds = metrics.histogram(
            "hades_scanner_analysis_seconds",
            "Wall-clock time to fully process one token",
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
        )
        self.stage_seconds = metrics.histogram(
            "hades_scanner_stage_seconds",
            "Wall-clock time per pipeline stage",
            ("stage",),
        )
        # The two clocks the acquisition path was missing. `analysis_seconds`
        # starts when a candidate *leaves the queue* and stops at
        # TokenDiscovered, so on its own it measures the fastest part of the
        # journey and would show a gain from work that changed nothing.
        self.discovery_lag_seconds = metrics.histogram(
            "hades_scanner_discovery_lag_seconds",
            "Age of a token when we first saw it (created_at → discovered)",
            ("source",),
            # A 5s poll interval puts the floor near 2.5s on average; the upper
            # buckets exist because a source can surface a token minutes late.
            buckets=(1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0, 900.0),
        )
        self.discovered_to_features_seconds = metrics.histogram(
            "hades_scanner_discovered_to_features_seconds",
            "End-to-end time from TokenDiscovered to features ready",
            buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
        )
        self.queue_depth = metrics.gauge(
            "hades_scanner_queue_depth",
            "Current depth of the acquisition processing queue",
        )
        self.active_workers = metrics.gauge(
            "hades_scanner_active_workers",
            "Pipeline workers currently processing a token",
        )
        self.sources_available = metrics.gauge(
            "hades_scanner_sources_available",
            "1 if a discovery source is currently reachable, else 0",
            ("source",),
        )
        self.tokens_active = metrics.gauge(
            "hades_scanner_tokens_active",
            "Distinct tokens seen within the active window",
        )
