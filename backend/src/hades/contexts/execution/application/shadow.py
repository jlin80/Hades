"""Shadow Executor — runs a candidate adapter alongside the real one, for free.

The primary adapter's fill is the *only* one that ever leaves this class. The
candidate runs concurrently, its result is recorded for comparison, and then it
is discarded. Nothing downstream — Portfolio, Learning, the dashboard — can tell
a shadow comparison happened.

**The safety rule that shapes this class.** A shadow run *executes* the
candidate. If the candidate were a live adapter, shadowing it would sign and
send a second, real, unaccounted-for transaction for every order — the platform
would silently double-trade. So the candidate is rejected at construction if it
reports ``mode == "live"``. This is a hard invariant, not a configuration: there
is no flag that permits it, because there is no correct value for that flag.

The consequence is worth stating plainly rather than working around: **the
live-transport comparison the fast path was built for cannot run in paper**, and
this harness cannot pretend otherwise. What it can compare today is any two
non-fund-moving adapters — and what it will compare, once a quote provider and a
signer exist, is a live primary against a live candidate *in live mode*, which is
a decision well outside this code. See ``docs/EXECUTION_FAST_PATH_2026-08-04.md``.

Two further properties keep the hot path safe:

- **The candidate can never fail the order.** Any exception it raises is caught
  and recorded; the primary's fill is returned regardless.
- **The candidate can never stall the order.** It runs under a timeout; if it
  overruns it is cancelled and recorded as such. A slow shadow must cost
  latency measurements, never trades.

Off by default (``EXECUTION_SHADOW_ENABLED``).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from hades.contexts.execution.domain.models import (
    ExecutionMode,
    FillReport,
    OrderRequest,
)
from hades.contexts.execution.domain.ports import Executor
from hades.shared_kernel.logging import get_logger

_logger = get_logger("execution.shadow")

#: A shadow run that overruns this is cancelled rather than allowed to delay the
#: order. Generous relative to a fill, because a slow candidate is itself the
#: finding — but bounded, because the hot path is not negotiable.
DEFAULT_SHADOW_TIMEOUT_SECONDS = 30.0


class ShadowSafetyError(RuntimeError):
    """Raised when a candidate could move real funds. Never catch this to proceed."""


@dataclass
class ShadowComparison:
    """One primary-vs-candidate observation. Pure telemetry, never a decision."""

    primary_filled: bool = False
    candidate_filled: bool = False
    primary_latency_ms: int = 0
    candidate_latency_ms: int = 0
    candidate_route: str = "unknown"
    #: Populated when the candidate raised, timed out, or returned a failure.
    candidate_error: str | None = None

    @property
    def comparable(self) -> bool:
        """Only a run where *both* sides filled says anything about latency."""
        return self.primary_filled and self.candidate_filled

    @property
    def delta_ms(self) -> int | None:
        """Candidate minus primary. Negative means the candidate was faster."""
        if not self.comparable:
            return None
        return self.candidate_latency_ms - self.primary_latency_ms


@dataclass
class ShadowStats:
    """Running tally, so the comparison is readable without a metrics stack."""

    runs: int = 0
    comparable: int = 0
    candidate_failures: int = 0
    candidate_timeouts: int = 0
    _deltas: list[int] = field(default_factory=list)

    def record(self, comparison: ShadowComparison) -> None:
        self.runs += 1
        if comparison.candidate_error is not None:
            self.candidate_failures += 1
            if "timed out" in comparison.candidate_error:
                self.candidate_timeouts += 1
        delta = comparison.delta_ms
        if delta is not None:
            self.comparable += 1
            self._deltas.append(delta)

    @property
    def candidate_fill_rate(self) -> float:
        """Share of runs where the candidate produced a fill. 0.0 with no runs."""
        return (self.comparable / self.runs) if self.runs else 0.0

    @property
    def median_delta_ms(self) -> int | None:
        """Median candidate-minus-primary latency over comparable runs.

        Median, not mean: landing latency has a long right tail (a retry, a
        confirmation that nearly timed out), and a mean would let one outlier
        decide whether a paid route looks worth its tip.
        """
        if not self._deltas:
            return None
        ordered = sorted(self._deltas)
        mid = len(ordered) // 2
        if len(ordered) % 2 == 1:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) // 2


class ShadowExecutor:
    """Executes with ``primary`` and measures ``candidate``. Satisfies ``Executor``.

    Reports the *primary's* mode, because that is the mode the order actually
    executes in — the engine must route and label it exactly as if the shadow
    were not there.
    """

    def __init__(
        self,
        *,
        primary: Executor,
        candidate: Executor,
        timeout_seconds: float = DEFAULT_SHADOW_TIMEOUT_SECONDS,
    ) -> None:
        if candidate.mode == ExecutionMode.LIVE.value:
            # Shadowing a live adapter means signing and sending a second real
            # transaction per order. There is no configuration under which that
            # is acceptable, so it is refused here and not behind a flag.
            raise ShadowSafetyError(
                "a live adapter can never be a shadow candidate: it would submit "
                "a second real transaction for every order"
            )
        self._primary = primary
        self._candidate = candidate
        self._timeout = max(0.1, timeout_seconds)
        self._stats = ShadowStats()

    @property
    def mode(self) -> str:
        return self._primary.mode

    @property
    def stats(self) -> ShadowStats:
        return self._stats

    async def execute(self, request: OrderRequest) -> FillReport:
        primary_task = asyncio.create_task(self._primary.execute(request))
        candidate_task = asyncio.create_task(self._run_candidate(request))

        # The primary is awaited first and on its own: whatever happens to the
        # candidate, the order's outcome is already settled by this line.
        fill = await primary_task
        candidate_fill, candidate_error = await candidate_task

        comparison = ShadowComparison(
            primary_filled=fill.filled,
            candidate_filled=candidate_fill is not None and candidate_fill.filled,
            primary_latency_ms=fill.latency_ms,
            candidate_latency_ms=candidate_fill.latency_ms if candidate_fill else 0,
            candidate_route=_route_of(candidate_fill, self._candidate),
            candidate_error=candidate_error
            or (candidate_fill.error if candidate_fill and not candidate_fill.filled else None),
        )
        self._stats.record(comparison)
        _log_comparison(request, comparison, self._stats)
        return fill

    async def _run_candidate(self, request: OrderRequest) -> tuple[FillReport | None, str | None]:
        """Run the candidate. Never raises — a shadow failure is a datum, not an error."""
        started = time.monotonic()
        try:
            fill = await asyncio.wait_for(self._candidate.execute(request), timeout=self._timeout)
        except TimeoutError:
            elapsed = int((time.monotonic() - started) * 1000)
            return None, f"shadow candidate timed out after {elapsed}ms"
        except asyncio.CancelledError:  # shutdown — propagate, never swallow
            raise
        except Exception as exc:
            return None, f"shadow candidate raised: {exc}"
        return fill, None


def _route_of(fill: FillReport | None, candidate: Executor) -> str:
    if fill is not None and fill.latency is not None:
        return fill.latency.route
    route = getattr(candidate, "route", None)
    return route if isinstance(route, str) else "unknown"


def _log_comparison(
    request: OrderRequest, comparison: ShadowComparison, stats: ShadowStats
) -> None:
    """One line per order. The shadow is worthless if nobody can read its result."""
    _logger.info(
        "shadow_comparison",
        mint=str(request.token.mint),
        side=request.side.value,
        route=comparison.candidate_route,
        primary_filled=comparison.primary_filled,
        candidate_filled=comparison.candidate_filled,
        primary_latency_ms=comparison.primary_latency_ms,
        candidate_latency_ms=comparison.candidate_latency_ms,
        delta_ms=comparison.delta_ms,
        candidate_error=comparison.candidate_error,
        runs=stats.runs,
        candidate_fill_rate=round(stats.candidate_fill_rate, 4),
        median_delta_ms=stats.median_delta_ms,
    )
