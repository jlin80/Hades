"""Redis Streams event-bus transport.

Selected when ``EVENT_BUS_TRANSPORT=redis``. It gives Hades a durable,
multi-service event backbone with the same :class:`EventBus` interface as the
in-memory bus, so no publisher or subscriber changes when a context moves to its
own container.

Semantics:
- **Publish** ``XADD`` s the event envelope to one stream.
- **Consume** uses a per-service **consumer group** (so every service sees every
  event — each group gets its own copy) and a consumer named after the instance.
- Delivery is **at-least-once**; handlers must be idempotent (same contract as
  the in-memory bus). Messages are ``XACK`` ed only after handlers run.

The consumer loop runs via :meth:`run` and is launched by the background process
that owns the bus (worker / engine / watchdog / notification / scheduler).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import orjson

from hades.shared_kernel.cache.redis_provider import RedisProvider
from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.events.bus import EventHandler
from hades.shared_kernel.events.registry import EventRegistry
from hades.shared_kernel.logging import get_logger

_logger = get_logger("events.redis_bus")


class RedisEventBus:
    """Durable event bus over a single Redis Stream + per-service group."""

    def __init__(
        self,
        provider: RedisProvider,
        registry: EventRegistry,
        *,
        stream_prefix: str = "hades.events",
        group: str = "default",
        consumer: str = "consumer-1",
        block_ms: int = 2000,
        batch: int = 50,
        max_len: int = 250_000,
        lag_warn_threshold: int = 5_000,
        lag_check_interval_seconds: float = 60.0,
        reclaim_after_seconds: float = 300.0,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._stream = f"{stream_prefix}:stream"
        self._group = group
        self._consumer = consumer
        self._block_ms = block_ms
        self._batch = batch
        self._handlers: dict[str, list[EventHandler]] = {}
        self._stop = asyncio.Event()
        self._group_ready = False
        self._max_len = max(1_000, max_len)
        self._lag_warn = max(1, lag_warn_threshold)
        self._lag_interval = max(5.0, lag_check_interval_seconds)
        self._last_lag_check = 0.0
        # How long a delivered-but-unacked message may sit before another
        # consumer takes it over. Comfortably longer than any handler chain,
        # short enough that a crashed process does not strand work for a day.
        self._reclaim_after_ms = int(max(30.0, reclaim_after_seconds) * 1000)
        self._reclaim_cursor = "0-0"

    # --- EventBus interface ---------------------------------------------------
    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)
        _logger.debug("handler_subscribed", event_type=event_type, group=self._group)

    async def publish(self, event: DomainEvent) -> None:
        envelope = event.to_envelope()
        # ``maxlen`` with ``approximate`` is what keeps the stream bounded. Without
        # it the stream grows for as long as the platform runs: a live deployment
        # reached 172k entries and 155 MB of Redis with no ``maxmemory`` set, which
        # ends in an eviction or an OOM rather than in a warning. Approximate
        # trimming lets Redis drop whole radix nodes, so the cost per publish is
        # negligible — and the cap is far above any consumer's working set, so a
        # briefly-behind consumer is never trimmed out from under it.
        await self._provider.client().xadd(
            self._stream,
            {"type": event.event_type, "data": orjson.dumps(envelope).decode()},
            maxlen=self._max_len,
            approximate=True,
        )

    async def publish_many(self, events: list[DomainEvent]) -> None:
        for event in events:
            await self.publish(event)

    # --- consumer lifecycle ---------------------------------------------------
    async def _ensure_group(self) -> None:
        if self._group_ready:
            return
        try:
            await self._provider.client().xgroup_create(
                self._stream, self._group, id="$", mkstream=True
            )
        except Exception as exc:  # BUSYGROUP means it already exists
            if "BUSYGROUP" not in str(exc):
                raise
        self._group_ready = True

    async def run(self) -> None:
        """Consume the stream and dispatch to handlers until :meth:`stop`."""
        await self._ensure_group()
        client = self._provider.client()
        _logger.info("redis_bus_consuming", stream=self._stream, group=self._group)
        while not self._stop.is_set():
            try:
                response = await client.xreadgroup(
                    self._group,
                    self._consumer,
                    {self._stream: ">"},
                    count=self._batch,
                    block=self._block_ms,
                )
            except Exception as exc:  # keep the loop alive on transient errors
                _logger.warning("redis_bus_read_failed", error=str(exc))
                await asyncio.sleep(1.0)
                continue
            await self._check_lag()
            await self._reclaim_stale()
            if not response:
                continue
            for _stream, messages in response:
                for message_id, fields in messages:
                    await self._dispatch(fields)
                    await client.xack(  # type: ignore[no-untyped-call]
                        self._stream, self._group, message_id
                    )

    async def _reclaim_stale(self) -> None:
        """Take over messages a previous consumer was delivered and never acked.

        A consumer group's ``pending`` list is per-consumer, and the consumer name
        is this process's stable instance id. So a container killed mid-dispatch —
        a deploy, an OOM, a `docker compose up` that recreates it — leaves its
        in-flight messages delivered to a name that will never ack them. Nothing
        redelivers those on its own: ``XREADGROUP >`` only ever returns *new*
        entries. They simply stop existing as far as the platform is concerned.

        The live deployment carried 765 such messages, accumulated across
        restarts, and they were invisible: the events had been published, the
        stream still held them, and no handler had run. A gap in the middle of a
        history that looks complete is worse than a gap you can see.

        ``XAUTOCLAIM`` is exactly the right primitive: it hands us any message
        idle longer than the threshold, we dispatch and ack it normally, and the
        cursor walks the pending list across calls. Handlers are already required
        to be idempotent — the bus has always been at-least-once — so a reclaim
        that duplicates work is safe by contract.
        """
        try:
            result = await self._provider.client().xautoclaim(
                self._stream,
                self._group,
                self._consumer,
                min_idle_time=self._reclaim_after_ms,
                start_id=self._reclaim_cursor,
                count=self._batch,
            )
        except Exception as exc:  # reclaiming is best-effort, never fatal
            _logger.debug("redis_bus_reclaim_failed", error=str(exc))
            return

        # redis-py returns (next_cursor, claimed[, deleted]) depending on version.
        if not isinstance(result, (list, tuple)) or len(result) < 2:
            return
        self._reclaim_cursor = str(result[0]) if result[0] else "0-0"
        claimed = result[1] or []
        if not claimed:
            return

        _logger.warning(
            "redis_bus_reclaimed",
            group=self._group,
            count=len(claimed),
            idle_ms=self._reclaim_after_ms,
            note="messages a previous consumer never acked; replaying them now",
        )
        client = self._provider.client()
        for message_id, fields in claimed:
            await self._dispatch(fields)
            await client.xack(self._stream, self._group, message_id)  # type: ignore[no-untyped-call]

    async def _check_lag(self) -> None:
        """Warn when this consumer group falls behind the stream.

        A backlogged consumer is the most dangerous silent state this platform
        has. One deployment ran **59 hours** behind: the decision path was
        judging tokens from two days earlier, the portfolio's mark-to-market
        events had not been reached so its unrealised PnL sat at exactly zero,
        and every container reported healthy the whole time. Nothing anywhere
        measured the gap, so the only evidence was a number nobody was reading.

        Cheap by construction: one ``XINFO GROUPS`` per interval, and any failure
        is swallowed — observability must never be able to stall the bus it
        observes.
        """
        now = time.monotonic()
        if now - self._last_lag_check < self._lag_interval:
            return
        self._last_lag_check = now
        try:
            groups = await self._provider.client().xinfo_groups(  # type: ignore[no-untyped-call]
                self._stream
            )
        except Exception as exc:  # never let the watchdog break the thing watched
            _logger.debug("redis_bus_lag_check_failed", error=str(exc))
            return
        for group in groups:
            if group.get("name") != self._group:
                continue
            lag = group.get("lag")
            pending = group.get("pending", 0)
            if isinstance(lag, int) and lag > self._lag_warn:
                _logger.error(
                    "redis_bus_consumer_behind",
                    group=self._group,
                    lag=lag,
                    pending=pending,
                    threshold=self._lag_warn,
                    note=(
                        "this service is processing stale events; anything it decides "
                        "is based on the past, and it will look healthy while doing so"
                    ),
                )
            else:
                _logger.debug("redis_bus_lag", group=self._group, lag=lag, pending=pending)
            return

    async def _dispatch(self, fields: dict[str, Any]) -> None:
        event_type = fields.get("type", "")
        handlers = self._handlers.get(event_type)
        if not handlers:
            return
        try:
            envelope = orjson.loads(fields["data"])
        except Exception as exc:
            _logger.error("redis_bus_bad_envelope", error=str(exc))
            return
        event = self._registry.rebuild(envelope)
        if event is None:
            _logger.debug("redis_bus_unknown_event", event_type=event_type)
            return
        for handler in handlers:
            try:
                await handler(event)
            except Exception as exc:  # one bad handler must not block others
                _logger.error("event_handler_failed", event_type=event_type, error=str(exc))

    def stop(self) -> None:
        self._stop.set()
