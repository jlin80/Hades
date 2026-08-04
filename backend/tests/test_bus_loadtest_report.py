"""The load harness's verdict logic — the part that must not be wrong.

The harness itself needs a real Redis (it is run as a module against a throwaway
instance). What is tested here without one is the reasoning it applies to the
numbers, because a load test that mis-reads its own results is worse than none:
it would report a healthy ceiling while the consumer was falling behind.
"""

from __future__ import annotations

from hades.ops.bus_loadtest import LoadReport, RateResult, _Counter, _event


def _result(
    *,
    multiplier: int,
    published: int,
    consumed: int,
    cycles: int = 2,
    backlog: int = 0,
    publish_seconds: float = 10.0,
) -> RateResult:
    return RateResult(
        multiplier=multiplier,
        target_eps=20.0 * multiplier,
        seconds=10.0,
        published=published,
        consumed=consumed,
        cycles=cycles,
        backlog=backlog,
        publish_seconds=publish_seconds,
    )


def test_keeping_up_requires_both_full_consumption_and_a_turning_loop() -> None:
    assert _result(multiplier=1, published=200, consumed=200).kept_up is True
    # Consumed everything, but the loop never turned — the four-day-outage shape.
    assert _result(multiplier=1, published=200, consumed=200, cycles=0).kept_up is False


def test_falling_behind_is_not_kept_up() -> None:
    assert _result(multiplier=10, published=2000, consumed=1500).kept_up is False


def test_a_tiny_shortfall_is_tolerated_but_a_real_one_is_not() -> None:
    """At-least-once delivery and timing jitter make exact equality the wrong bar."""
    assert _result(multiplier=5, published=1000, consumed=995).kept_up is True
    assert _result(multiplier=5, published=1000, consumed=980).kept_up is False


def test_the_ceiling_is_the_first_rate_that_failed() -> None:
    report = LoadReport(
        results=[
            _result(multiplier=1, published=200, consumed=200),
            _result(multiplier=5, published=1000, consumed=1000),
            _result(multiplier=10, published=2000, consumed=1200, backlog=800),
        ]
    )
    ceiling = report.ceiling
    assert ceiling is not None
    assert ceiling.multiplier == 10
    assert ceiling.backlog == 800


def test_no_ceiling_when_every_rate_kept_up() -> None:
    report = LoadReport(
        results=[
            _result(multiplier=1, published=200, consumed=200),
            _result(multiplier=10, published=2000, consumed=2000),
        ]
    )
    assert report.ceiling is None


def test_publish_rate_is_reported_from_measured_time_not_the_target() -> None:
    """If publishing itself could not hit the target, the row must show that."""
    result = _result(multiplier=10, published=1000, consumed=1000)
    result.publish_seconds = 20.0  # took twice as long as intended
    assert result.achieved_publish_eps == 50.0
    assert result.target_eps == 200.0
    assert result.as_row()["achieved_publish_eps"] == 50.0


def test_zero_publish_time_does_not_divide_by_zero() -> None:
    result = RateResult(multiplier=1, target_eps=20.0, seconds=1.0)
    assert result.achieved_publish_eps == 0.0
    assert result.consumed_ratio == 0.0


# -- the distinction the first run got wrong ----------------------------------


def test_a_consumer_is_not_credited_for_a_load_that_was_never_applied() -> None:
    """The 10x run consumed 100% — of the 29 eps the publisher managed, not 200."""
    # Asked for 200 eps; took 69s to publish 2000, i.e. ~29 eps.
    result = _result(multiplier=10, published=2000, consumed=2000, publish_seconds=69.0)
    assert result.kept_up is True  # it did consume everything it was given
    assert result.load_applied is False  # but it was never given 10x
    assert result.verdict == "publish_ceiling"


def test_a_genuinely_sustained_rate_says_so() -> None:
    result = _result(multiplier=5, published=1000, consumed=1000, publish_seconds=10.0)
    assert result.load_applied is True
    assert result.verdict == "sustained"


def test_a_consumer_falling_behind_outranks_a_publish_shortfall() -> None:
    result = _result(multiplier=10, published=2000, consumed=1000, publish_seconds=69.0)
    assert result.verdict == "consumer_fell_behind"


def test_publish_ceiling_is_reported_separately_from_the_consumer_ceiling() -> None:
    report = LoadReport(
        results=[
            _result(multiplier=1, published=200, consumed=200, publish_seconds=10.0),
            _result(multiplier=10, published=2000, consumed=2000, publish_seconds=69.0),
        ]
    )
    assert report.ceiling is None, "the consumer never fell behind"
    assert report.publish_ceiling is not None
    assert report.publish_ceiling.multiplier == 10


def test_max_sustained_eps_is_the_measured_rate_not_the_target() -> None:
    report = LoadReport(
        results=[
            _result(multiplier=1, published=200, consumed=200, publish_seconds=10.0),
            _result(multiplier=10, published=2000, consumed=2000, publish_seconds=69.0),
        ]
    )
    # ~29 eps actually achieved beats the 20 eps baseline; 200 was never reached.
    assert 28.0 <= report.max_sustained_eps <= 30.0


def test_a_loop_that_turned_once_and_died_is_not_keeping_up() -> None:
    """A check that only asked 'did it ever turn?' would pass the outage shape."""
    assert _result(multiplier=1, published=200, consumed=200, cycles=1).kept_up is True
    assert _result(multiplier=1, published=200, consumed=200, cycles=0).kept_up is False


async def test_the_injected_failure_handler_fails_on_schedule() -> None:
    """The harness must actually exercise the poison-message guard, not claim to."""
    counter = _Counter(fail_every=3)
    raised = 0
    for _ in range(9):
        try:
            await counter.handle(_event())
        except RuntimeError:
            raised += 1
    assert counter.count == 9
    assert raised == 3
    assert counter.failures == 3


async def test_failures_can_be_disabled() -> None:
    counter = _Counter(fail_every=0)
    for _ in range(5):
        await counter.handle(_event())
    assert counter.failures == 0
