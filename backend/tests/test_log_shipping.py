"""Cross-process log shipping — the dashboard must see more than the API.

The Worker hosts the entire scanner → security → intelligence → committee →
strategy → risk → execution pipeline, but the terminal's ring buffer is
process-local, so the UI could only ever show the API's own lines. These tests pin
the bridge that fixes that, and above all pin the property that matters most:
**logging must never block, break or bring down a caller**, whatever Redis does.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from hades.shared_kernel.logging.ring_buffer import LogRingBuffer
from hades.shared_kernel.logging.setup import set_log_shipper
from hades.shared_kernel.logging.shipper import (
    LOG_STREAM_KEY,
    STAGING_MAXLEN,
    LogShipper,
    LogTailer,
)


class _FakeRedis:
    """Minimal stand-in for the Redis Stream commands the shipper/tailer use."""

    def __init__(self, *, fail: bool = False) -> None:
        self.entries: list[tuple[str, dict[str, str]]] = []
        self.fail = fail
        self._seq = 0

    # -- pipeline -------------------------------------------------------------
    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)

    def _add(self, fields: dict[str, str]) -> None:
        self._seq += 1
        self.entries.append((f"{self._seq}-0", fields))

    async def xread(
        self, streams: dict[str, str], count: int = 10, block: int = 0
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        if self.fail:
            raise ConnectionError("redis is down")
        last = streams[LOG_STREAM_KEY]
        start = 0 if last == "$" else next(
            (i + 1 for i, (eid, _) in enumerate(self.entries) if eid == last), 0
        )
        batch = self.entries[start : start + count]
        if not batch:
            await asyncio.sleep(0)
            return []
        return [(LOG_STREAM_KEY, batch)]


class _FakePipeline:
    def __init__(self, redis: _FakeRedis) -> None:
        self._redis = redis
        self._queued: list[dict[str, str]] = []

    def xadd(self, _key: str, fields: dict[str, str], **_kwargs: Any) -> None:
        self._queued.append(fields)

    async def execute(self) -> None:
        if self._redis.fail:
            raise ConnectionError("redis is down")
        for fields in self._queued:
            self._redis._add(fields)


class _FakeProvider:
    def __init__(self, redis: _FakeRedis) -> None:
        self._redis = redis

    def client(self) -> _FakeRedis:
        return self._redis


def _shipper(redis: _FakeRedis, role: str = "worker") -> LogShipper:
    return LogShipper(_FakeProvider(redis), role=role)  # type: ignore[arg-type]


# -- shipping -----------------------------------------------------------------


async def test_shipped_records_reach_the_stream_tagged_with_their_role() -> None:
    redis = _FakeRedis()
    shipper = _shipper(redis)
    shipper.submit({"event": "scanner_candidate", "mint": "abc"})

    await shipper.start()
    await asyncio.sleep(0.4)
    await shipper.stop()

    assert len(redis.entries) == 1
    record = json.loads(redis.entries[0][1]["record"])
    assert record["event"] == "scanner_candidate"
    # Without the role a merged stream is unreadable — you cannot tell which
    # process produced a line.
    assert record["role"] == "worker"


def test_submitting_never_blocks_and_never_needs_a_running_shipper() -> None:
    # The processor calls submit() from whatever thread logged, often inside a hot
    # loop. It must be safe with no event loop and no Redis anywhere.
    shipper = _shipper(_FakeRedis())
    for i in range(10):
        shipper.submit({"event": f"line-{i}"})

    assert shipper.dropped == 0


def test_a_flood_drops_oldest_records_rather_than_growing_without_bound() -> None:
    shipper = _shipper(_FakeRedis())
    for i in range(STAGING_MAXLEN + 50):
        shipper.submit({"event": f"line-{i}"})

    # Bounded memory is the invariant; losing old lines beats stalling the engine.
    assert shipper.dropped == 50


async def test_a_redis_outage_does_not_raise_and_does_not_wedge_the_shipper() -> None:
    redis = _FakeRedis(fail=True)
    shipper = _shipper(redis)
    shipper.submit({"event": "line"})

    await shipper.start()
    await asyncio.sleep(0.4)
    await shipper.stop()

    # Records are dropped, the process lives, and nothing propagated to a caller.
    assert shipper.dropped >= 1
    assert redis.entries == []


# -- tailing ------------------------------------------------------------------


async def test_the_tailer_merges_other_processes_into_the_local_buffer() -> None:
    redis = _FakeRedis()
    worker = _shipper(redis, role="worker")
    worker.submit({"event": "committee_prediction", "mint": "xyz"})
    await worker.start()
    await asyncio.sleep(0.4)
    await worker.stop()

    buffer = LogRingBuffer()
    tailer = LogTailer(_FakeProvider(redis), own_role="api")  # type: ignore[arg-type]
    await tailer.start(buffer)
    await asyncio.sleep(0.4)
    await tailer.stop()

    _seq, records = buffer.recent()
    assert [r["event"] for r in records] == ["committee_prediction"]


async def test_the_tailer_skips_its_own_lines_so_they_are_not_shown_twice() -> None:
    redis = _FakeRedis()
    api = _shipper(redis, role="api")
    api.submit({"event": "api_started"})
    await api.start()
    await asyncio.sleep(0.4)
    await api.stop()

    buffer = LogRingBuffer()
    tailer = LogTailer(_FakeProvider(redis), own_role="api")  # type: ignore[arg-type]
    await tailer.start(buffer)
    await asyncio.sleep(0.4)
    await tailer.stop()

    # The API's own lines are already in its local buffer.
    assert buffer.recent()[1] == []


async def test_the_tailer_survives_a_redis_outage() -> None:
    buffer = LogRingBuffer()
    tailer = LogTailer(_FakeProvider(_FakeRedis(fail=True)))  # type: ignore[arg-type]

    await tailer.start(buffer)
    await asyncio.sleep(0.3)
    await tailer.stop()

    assert buffer.recent()[1] == []


# -- the processor hook -------------------------------------------------------


def test_a_broken_shipper_cannot_break_the_logging_call_site() -> None:
    from hades.shared_kernel.logging.setup import _ring_buffer_processor

    class _Exploding:
        def submit(self, _record: dict[str, Any]) -> None:
            raise RuntimeError("boom")

    set_log_shipper(_Exploding())
    try:
        # A processor that propagates would break the very call it only observes.
        result = _ring_buffer_processor(None, "info", {"event": "still fine"})
    finally:
        set_log_shipper(None)

    assert result["event"] == "still fine"


@pytest.mark.parametrize("payload", ["not json", "", "[1,2,3]"])
async def test_malformed_stream_entries_are_skipped_not_crashed_on(payload: str) -> None:
    redis = _FakeRedis()
    redis._add({"record": payload})
    buffer = LogRingBuffer()
    tailer = LogTailer(_FakeProvider(redis))  # type: ignore[arg-type]

    await tailer.start(buffer)
    await asyncio.sleep(0.3)
    await tailer.stop()

    assert buffer.recent()[1] == []
