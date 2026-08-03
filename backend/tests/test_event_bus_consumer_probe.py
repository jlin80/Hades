"""The probe that would have caught a four-day outage on day one.

The Worker's event-bus consumer died thirty minutes after start-up. For the next
four days Postgres answered, Redis pinged, the API served ``/health`` and every
liveness file stayed fresh — all truthfully, because the process really was
alive. It just was not listening, and nothing anywhere measured that.

Redis knows, though, and it knows across processes: a consumer group's ``idle``
is the time since that consumer last talked to the stream. A live loop blocks for
at most a couple of seconds per read, so these tests pin the distinction between
a consumer that is quiet and one that is gone.
"""

from __future__ import annotations

from hades.contexts.monitoring.domain.models import HealthStatus
from hades.contexts.monitoring.infrastructure.probes import EventBusConsumerProbe


class _FakeClient:
    def __init__(self, consumers: dict[str, list[dict[str, object]]]) -> None:
        self._consumers = consumers

    async def xinfo_groups(self, stream: str) -> list[dict[str, object]]:
        return [{"name": name} for name in self._consumers]

    async def xinfo_consumers(self, stream: str, group: str) -> list[dict[str, object]]:
        return self._consumers[group]


class _MissingStreamClient:
    async def xinfo_groups(self, stream: str) -> list[dict[str, object]]:
        raise RuntimeError("ERR no such key")


class _FakeProvider:
    def __init__(self, client: object) -> None:
        self._client = client

    def client(self) -> object:
        return self._client


def _probe(client: object, max_idle_seconds: float = 300.0) -> EventBusConsumerProbe:
    return EventBusConsumerProbe(
        _FakeProvider(client),  # type: ignore[arg-type]
        max_idle_seconds=max_idle_seconds,
    )


async def test_a_reading_consumer_is_healthy() -> None:
    probe = _probe(_FakeClient({"worker": [{"name": "w1", "idle": 1_200}]}))

    result = await probe.check()

    assert result.status is HealthStatus.HEALTHY


async def test_a_consumer_idle_for_days_is_unhealthy() -> None:
    """The exact production state: four days of idle, everything else green."""
    four_days_ms = 4 * 24 * 60 * 60 * 1000
    probe = _probe(_FakeClient({"worker": [{"name": "hades-local-1", "idle": four_days_ms}]}))

    result = await probe.check()

    assert result.status is HealthStatus.UNHEALTHY, (
        "a consumer that has not read for four days must never report healthy"
    )
    assert "worker" in result.detail


async def test_one_dead_group_condemns_the_whole_probe() -> None:
    """The Worker hosts the decision path; a healthy Engine does not cover it."""
    probe = _probe(
        _FakeClient(
            {
                "engine": [{"name": "e1", "idle": 500}],
                "worker": [{"name": "w1", "idle": 999_999}],
            }
        )
    )

    result = await probe.check()

    assert result.status is HealthStatus.UNHEALTHY
    assert "worker" in result.detail
    assert "engine" not in result.detail


async def test_a_group_with_no_consumer_at_all_is_unhealthy() -> None:
    probe = _probe(_FakeClient({"worker": []}))

    result = await probe.check()

    assert result.status is HealthStatus.UNHEALTHY


async def test_a_group_is_reading_if_any_of_its_consumers_is() -> None:
    probe = _probe(
        _FakeClient({"worker": [{"name": "stale", "idle": 999_999}, {"name": "live", "idle": 900}]})
    )

    result = await probe.check()

    assert result.status is HealthStatus.HEALTHY


async def test_a_fresh_deployment_without_a_stream_is_not_a_fault() -> None:
    probe = _probe(_MissingStreamClient())

    result = await probe.check()

    assert result.status is HealthStatus.HEALTHY
