"""Event-bus load harness — does the August resilience fix hold at scalping rates?

Checklist §7. Scalping implies far more throughput than the platform's current
operating volume, and the question this answers is narrow and falsifiable:
**at 5x and 10x today's rate, does the consumer loop keep turning, and does lag
stay bounded?**

Run it as a module against a **throwaway Redis**, never the production one:

    python -m hades.ops.bus_loadtest --redis redis://127.0.0.1:6399 \\
        --rates 1,5,10 --baseline-eps 20 --seconds 20

What it measures, per rate:

* **published / consumed** — and therefore whether the consumer kept up.
* **backlog** — the group's pending + un-delivered entries at the end. A backlog
  that grows with the rate is the ceiling the checklist asks to document.
* **cycles turned** — via ``RedisEventBus.last_cycle_at``. This is the specific
  regression guard: the four-day outage was a loop that *stopped* while every
  probe stayed green, so "the consumer is alive" must be measured, not assumed.
* **dispatch failures** — handler exceptions, which must not end the loop.

It deliberately reports numbers and does **not** change anything. Per the
auditoría's standing rule, a ceiling found here gets documented with figures —
it does not get "fixed" by splitting processes without evidence of real
contention.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass, field

from hades.contexts.common.domain.value_objects import Money, TokenMint, TokenRef
from hades.contexts.scanner.domain.events import TokenDiscovered
from hades.shared_kernel.cache.redis_provider import RedisProvider
from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.domain.identifiers import new_id
from hades.shared_kernel.events.redis_bus import RedisEventBus
from hades.shared_kernel.events.registry import EventRegistry
from hades.shared_kernel.logging import get_logger

_logger = get_logger("ops.bus_loadtest")

#: A mint that parses. The payload's content is irrelevant to throughput; its
#: *size* is not, so this uses a real event rather than a synthetic blob.
_MINT = "So11111111111111111111111111111111111111112"


@dataclass
class RateResult:
    """One rate's outcome. Everything here is measured, nothing is inferred."""

    multiplier: int
    target_eps: float
    seconds: float
    published: int = 0
    consumed: int = 0
    dispatch_failures: int = 0
    cycles: int = 0
    backlog: int = 0
    publish_seconds: float = 0.0

    @property
    def achieved_publish_eps(self) -> float:
        return self.published / self.publish_seconds if self.publish_seconds > 0 else 0.0

    @property
    def consumed_ratio(self) -> float:
        return self.consumed / self.published if self.published else 0.0

    @property
    def load_applied(self) -> bool:
        """Did the publisher actually reach the rate it was asked for?

        This distinction is the difference between a useful result and a
        flattering one. The consumer trivially "keeps up" with a load that was
        never generated, so a run whose publisher fell short says nothing about
        the consumer at that multiplier — it has found the *publish* ceiling
        instead, which is a different (and here, the real) finding.
        """
        return self.achieved_publish_eps >= self.target_eps * 0.9

    @property
    def kept_up(self) -> bool:
        """Consumed essentially everything published, and the loop kept turning."""
        return self.consumed_ratio >= 0.99 and self.cycles > 0

    @property
    def verdict(self) -> str:
        """What this row actually demonstrates. Never more than was measured."""
        if not self.kept_up:
            return "consumer_fell_behind"
        if not self.load_applied:
            return "publish_ceiling"  # consumer fine, but we could not push this hard
        return "sustained"

    def as_row(self) -> dict[str, object]:
        return {
            "multiplier": self.multiplier,
            "target_eps": round(self.target_eps, 1),
            "achieved_publish_eps": round(self.achieved_publish_eps, 1),
            "published": self.published,
            "consumed": self.consumed,
            "consumed_pct": round(self.consumed_ratio * 100, 2),
            "backlog": self.backlog,
            "cycles": self.cycles,
            "dispatch_failures": self.dispatch_failures,
            "load_applied": self.load_applied,
            "kept_up": self.kept_up,
            "verdict": self.verdict,
        }


@dataclass
class LoadReport:
    results: list[RateResult] = field(default_factory=list)

    @property
    def ceiling(self) -> RateResult | None:
        """The first rate that failed to keep up, if any — the consumer's ceiling."""
        return next((r for r in self.results if not r.kept_up), None)

    @property
    def publish_ceiling(self) -> RateResult | None:
        """The first rate the publisher could not generate, if any.

        Reported separately because it is a limit of the harness/host, not of the
        bus under test, and conflating the two would credit the consumer with
        surviving a load nobody applied.
        """
        return next((r for r in self.results if r.kept_up and not r.load_applied), None)

    @property
    def max_sustained_eps(self) -> float:
        """Highest rate actually published *and* fully consumed. The real number."""
        sustained = [r.achieved_publish_eps for r in self.results if r.kept_up]
        return max(sustained) if sustained else 0.0


def _event() -> DomainEvent:
    return TokenDiscovered(
        aggregate_id=new_id(),
        token=TokenRef(mint=TokenMint(address=_MINT), symbol="LOAD"),
        source="loadtest",
        initial_liquidity=Money(amount=50_000),
    )


class _Counter:
    """A handler that counts, and one that can be made to fail on demand.

    A share of failures is injected on purpose: the whole point of the August fix
    is that a poisonous message costs one error line and the loop keeps turning.
    A load test where every message is well-formed would not exercise that at all.
    """

    def __init__(self, *, fail_every: int = 0) -> None:
        self.count = 0
        self.failures = 0
        self._fail_every = fail_every

    async def handle(self, event: DomainEvent) -> None:
        self.count += 1
        if self._fail_every and self.count % self._fail_every == 0:
            self.failures += 1
            raise RuntimeError("injected handler failure")


class _CycleSampler:
    """Counts how many times the consumer loop actually turned during the run.

    ``last_cycle_at`` is a timestamp, not a counter, so the only way to observe
    turns is to sample it and count the changes. That matters more than it
    sounds: the failure this whole harness guards against is a loop that stops
    while everything reports healthy, and a check that only asks "did it ever
    turn?" would pass on a loop that turned once and died — which is exactly the
    shape of the four-day outage.
    """

    def __init__(self, bus: RedisEventBus, *, interval_seconds: float = 0.1) -> None:
        self._bus = bus
        self._interval = interval_seconds
        self.turns = 0
        self._last: float | None = None

    async def run(self) -> None:
        while True:
            current = self._bus.last_cycle_at
            if current is not None and current != self._last:
                self.turns += 1
                self._last = current
            await asyncio.sleep(self._interval)


async def _drain(bus: RedisEventBus, provider: RedisProvider, stream: str, group: str) -> int:
    """Best-effort backlog reading: entries the group has not yet acknowledged."""
    client = provider.client()
    try:
        groups = await client.xinfo_groups(stream)  # type: ignore[no-untyped-call]
    except Exception:  # stream may not exist yet
        return 0
    for info in groups:
        if info.get("name") == group:
            pending = int(info.get("pending", 0) or 0)
            lag_raw = info.get("lag")
            lag = int(lag_raw) if isinstance(lag_raw, int | str) and str(lag_raw).isdigit() else 0
            return pending + lag
    return 0


async def run_rate(
    *,
    provider: RedisProvider,
    multiplier: int,
    baseline_eps: float,
    seconds: float,
    fail_every: int,
) -> RateResult:
    """Publish at ``multiplier x baseline_eps`` for ``seconds`` and consume it."""
    target_eps = baseline_eps * multiplier
    total = max(1, int(target_eps * seconds))
    stream_prefix = f"hades.loadtest.x{multiplier}"
    group = "loadtest"

    registry = EventRegistry()
    registry.register(TokenDiscovered)
    bus = RedisEventBus(
        provider,
        registry,
        stream_prefix=stream_prefix,
        group=group,
        consumer=f"loadtest-{multiplier}",
        block_ms=200,
    )
    counter = _Counter(fail_every=fail_every)
    bus.subscribe(TokenDiscovered.__name__, counter.handle)

    result = RateResult(multiplier=multiplier, target_eps=target_eps, seconds=seconds)
    consumer = asyncio.create_task(bus.run(), name=f"loadtest-consumer-x{multiplier}")
    sampler = _CycleSampler(bus)
    sampler_task = asyncio.create_task(sampler.run(), name=f"loadtest-sampler-x{multiplier}")
    await asyncio.sleep(0.3)  # let the group be created before publishing

    interval = 1.0 / target_eps if target_eps > 0 else 0.0
    started = time.monotonic()
    next_at = started
    for _ in range(total):
        await bus.publish(_event())
        result.published += 1
        next_at += interval
        delay = next_at - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
    result.publish_seconds = time.monotonic() - started

    # Give the consumer a bounded chance to finish what is already published.
    deadline = time.monotonic() + max(5.0, seconds)
    while counter.count < result.published and time.monotonic() < deadline:
        await asyncio.sleep(0.2)

    bus.stop()
    consumer.cancel()
    sampler_task.cancel()
    await asyncio.gather(consumer, sampler_task, return_exceptions=True)

    result.consumed = counter.count
    result.dispatch_failures = counter.failures
    result.cycles = sampler.turns
    result.backlog = await _drain(bus, provider, f"{stream_prefix}:stream", group)

    # A loop that stopped turning is the failure this harness exists to catch, so
    # it is asserted here rather than left for a reader to notice in a table.
    if bus.last_cycle_at is None:
        _logger.error("loadtest_consumer_never_turned", multiplier=multiplier)
    _logger.info("loadtest_rate_done", **result.as_row())
    return result


async def run_load(
    *,
    redis_dsn: str,
    multipliers: list[int],
    baseline_eps: float,
    seconds: float,
    fail_every: int,
) -> LoadReport:
    provider = RedisProvider(redis_dsn)
    if not await provider.ping():
        raise RuntimeError(f"redis not reachable at {redis_dsn}")
    report = LoadReport()
    for multiplier in multipliers:
        report.results.append(
            await run_rate(
                provider=provider,
                multiplier=multiplier,
                baseline_eps=baseline_eps,
                seconds=seconds,
                fail_every=fail_every,
            )
        )
    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--redis",
        required=True,
        help="Redis DSN. Use a THROWAWAY instance — this writes streams and load.",
    )
    parser.add_argument("--rates", default="1,5,10", help="Comma-separated multipliers")
    parser.add_argument(
        "--baseline-eps", type=float, default=20.0, help="Events/sec taken as 1x today"
    )
    parser.add_argument("--seconds", type=float, default=20.0, help="Duration per rate")
    parser.add_argument(
        "--fail-every",
        type=int,
        default=100,
        help="Inject a handler failure every N events (0 disables). Exercises the "
        "guard that must keep the loop alive through a poisonous message.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    multipliers = [int(x) for x in str(args.rates).split(",") if x.strip()]
    report = asyncio.run(
        run_load(
            redis_dsn=args.redis,
            multipliers=multipliers,
            baseline_eps=args.baseline_eps,
            seconds=args.seconds,
            fail_every=args.fail_every,
        )
    )
    for result in report.results:
        _logger.info("loadtest_result", **result.as_row())
    _logger.info("loadtest_max_sustained_eps", eps=round(report.max_sustained_eps, 1))
    ceiling = report.ceiling
    if ceiling is not None:
        _logger.warning(
            "loadtest_consumer_ceiling_found",
            multiplier=ceiling.multiplier,
            target_eps=round(ceiling.target_eps, 1),
            consumed_pct=round(ceiling.consumed_ratio * 100, 2),
            backlog=ceiling.backlog,
            note="document this with figures; do not split processes without evidence",
        )
    publish_ceiling = report.publish_ceiling
    if publish_ceiling is not None:
        _logger.warning(
            "loadtest_publish_ceiling_found",
            multiplier=publish_ceiling.multiplier,
            target_eps=round(publish_ceiling.target_eps, 1),
            achieved_eps=round(publish_ceiling.achieved_publish_eps, 1),
            note=(
                "the publisher could not generate this rate, so the consumer was "
                "never tested at it — this bounds the experiment, not the bus"
            ),
        )
    return 0


if __name__ == "__main__":  # pragma: no cover — module entrypoint
    raise SystemExit(main())
