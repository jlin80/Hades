"""Redis Streams event-bus transport.

Selected when ``EVENT_BUS_TRANSPORT=redis``. It gives Hades a durable,
multi-service event backbone with the same :class:`EventBus` interface as the
in-memory bus, so no publisher or subscriber changes when a context moves to its
own container.

Semantics:
- **Publish** ``XADD`` s the event envelope to one stream.
- **Consume** uses **consumer groups** (so every service sees every event — each
  group gets its own copy) and a consumer named after the instance.
- Delivery is **at-least-once**; handlers must be idempotent (same contract as
  the in-memory bus). Messages are ``XACK`` ed only after handlers run.

**Lanes: why one process has several consumer groups.** A process used to have
exactly one group and one read loop, and every handler in it ran in series —
one ``await`` per message, then one ``await`` per handler, then the ``XACK``.
That makes the whole process's throughput equal to its *slowest* handler chain,
and the Worker hosts twelve runtimes. Measured on the live deployment
2026-08-06 over 21 minutes: the Worker read 2.51 events/s against 2.72 produced,
so it sat **exactly one stream-window (~50,000 events, ~8 h) behind and was
losing ground** — its lag never moved off the window size, because the entries it
had not reached were being trimmed away unread. ``scheduler``, ``watchdog``,
``engine`` and ``notification`` all sat at lag 0 throughout. The bus was not the
constraint; the earlier 10x load test was right about that and measured the wrong
side of the problem.

A *lane* is a named subset of handlers with its own consumer group
(``<role>.<lane>``), its own read loop and its own pending list. A storage-bound
acquisition handler can now block only its own lane, not the Committee's. Order
is still preserved **within** a lane and is **not** guaranteed *between* lanes —
which is why a lane boundary belongs between runtimes that were already only
coupled through events, and never inside one.

Lanes are created with ``id="$"``, so a newly-added lane starts at the present
and never replays history it was not there for.

The consumer loops run via :meth:`run` and are launched by the background process
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

#: Handlers that name no lane share this one. Its group keeps the bare role name,
#: so an existing deployment's group is neither renamed nor orphaned by adding
#: lanes — the default lane picks up exactly where the single group left off.
DEFAULT_LANE = "main"


class _ConsumerLane:
    """One consumer group + read loop, owning a named subset of handlers."""

    def __init__(
        self,
        *,
        name: str,
        group: str,
        stream: str,
        consumer: str,
        provider: RedisProvider,
        registry: EventRegistry,
        stop: asyncio.Event,
        block_ms: int,
        batch: int,
        lag_warn: int,
        lag_interval: float,
        reclaim_after_ms: int,
    ) -> None:
        self.name = name
        self._group = group
        self._stream = stream
        self._consumer = consumer
        self._provider = provider
        self._registry = registry
        self._stop = stop
        self._block_ms = block_ms
        self._batch = batch
        self._lag_warn = lag_warn
        self._lag_interval = lag_interval
        self._reclaim_after_ms = reclaim_after_ms
        self._handlers: dict[str, list[EventHandler]] = {}
        self._group_ready = False
        self._reclaim_cursor = "0-0"
        self._last_lag_check = 0.0
        self._last_cycle_at: float | None = None

    @property
    def group(self) -> str:
        return self._group

    @property
    def last_cycle_at(self) -> float | None:
        return self._last_cycle_at

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

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
        """Consume the stream and dispatch to this lane's handlers until stopped.

        **Nothing inside this loop may end it.** That is not defensive style, it
        is the lesson of a four-day outage: the loop logged ``redis_bus_consuming``
        once, ran for thirty minutes, and stopped — with no error, no traceback
        and no restart. Every container stayed healthy, the dashboard stayed
        green, and the entire decision path simply stopped receiving events while
        the stream kept growing to its 250,000-entry cap.

        It went unseen because of how the failure composes. Only the
        ``xreadgroup`` call was guarded, so anything raised further down —
        rebuilding an envelope into a typed event, an ``XACK`` on a dropped
        connection — escaped the ``while`` and returned from ``run``. The task is
        held in ``ServiceProcess._tasks`` and never awaited, so asyncio's
        "Task exception was never retrieved" warning never fires either: that
        warning comes from the task's ``__del__``, and a referenced task is never
        collected. The exception had nowhere to be seen.

        So the guard wraps the *whole* cycle. A poisonous message costs one error
        line and the loop keeps turning; a broken connection costs a second of
        backoff. Recording ``_last_cycle_at`` on every turn is the other half: a
        loop that stops turning must become visible to ``/health``, because this
        failure proved that "the process is alive" says nothing about whether it
        is still listening.
        """
        await self._ensure_group()
        _logger.info("redis_bus_consuming", stream=self._stream, group=self._group, lane=self.name)
        while not self._stop.is_set():
            try:
                await self._consume_once()
            except asyncio.CancelledError:  # shutdown — the only way out
                raise
            except Exception as exc:  # nothing else may end this loop
                _logger.error(
                    "redis_bus_cycle_failed",
                    group=self._group,
                    lane=self.name,
                    error=str(exc),
                    error_type=type(exc).__name__,
                    note="consume cycle raised; the loop continues after a backoff",
                    exc_info=True,
                )
                await asyncio.sleep(1.0)

    async def _consume_once(self) -> None:
        """One read → dispatch → ack cycle. Raises only to :meth:`run`'s guard."""
        client = self._provider.client()
        response = await client.xreadgroup(
            self._group,
            self._consumer,
            {self._stream: ">"},
            count=self._batch,
            block=self._block_ms,
        )
        self._last_cycle_at = time.time()
        await self._check_lag()
        await self._reclaim_stale()
        if not response:
            return
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
            _logger.debug("redis_bus_reclaim_failed", lane=self.name, error=str(exc))
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
            lane=self.name,
            count=len(claimed),
            idle_ms=self._reclaim_after_ms,
            note="messages a previous consumer never acked; replaying them now",
        )
        client = self._provider.client()
        for message_id, fields in claimed:
            if not fields:
                # The message is in the pending list but no longer in the stream:
                # `MAXLEN` trimmed it away while it sat unacked. Redis hands these
                # back with empty fields, and dispatching one used to raise on
                # `fields.get(...)` and take the whole consumer loop with it —
                # which is how the reclaim added in the previous commit became a
                # *new* way for the Worker to stop consuming a few minutes after
                # every restart. There is nothing to replay; the entry is gone.
                # Ack it so it leaves the pending list instead of being reclaimed
                # forever.
                _logger.warning(
                    "redis_bus_reclaimed_message_gone",
                    group=self._group,
                    lane=self.name,
                    message_id=str(message_id),
                    note="pending entry was trimmed from the stream; acking to drop it",
                )
                await client.xack(self._stream, self._group, message_id)  # type: ignore[no-untyped-call]
                continue
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

        Per lane rather than per process, because that is the resolution the
        answer actually has: a process-wide number cannot say *which* handler
        chain is the slow one, and on 2026-08-06 that was the whole question.

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
            _logger.debug("redis_bus_lag_check_failed", lane=self.name, error=str(exc))
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
                    lane=self.name,
                    lag=lag,
                    pending=pending,
                    threshold=self._lag_warn,
                    note=(
                        "this service is processing stale events; anything it decides "
                        "is based on the past, and it will look healthy while doing so"
                    ),
                )
            else:
                _logger.debug(
                    "redis_bus_lag", group=self._group, lane=self.name, lag=lag, pending=pending
                )
            return

    async def _dispatch(self, fields: dict[str, Any]) -> None:
        event_type = fields.get("type", "")
        handlers = self._handlers.get(event_type)
        if not handlers:
            return
        try:
            envelope = orjson.loads(fields["data"])
        except Exception as exc:
            _logger.error("redis_bus_bad_envelope", lane=self.name, error=str(exc))
            return
        try:
            event = self._registry.rebuild(envelope)
        except Exception as exc:
            # A published envelope whose schema no longer validates is a bad
            # message, not a bad bus. Report it and let the caller ack it, so one
            # unparseable event cannot wedge the group behind it forever.
            _logger.error(
                "redis_bus_rebuild_failed",
                event_type=event_type,
                lane=self.name,
                error=str(exc),
            )
            return
        if event is None:
            _logger.debug("redis_bus_unknown_event", event_type=event_type, lane=self.name)
            return
        for handler in handlers:
            try:
                await handler(event)
            except Exception as exc:  # one bad handler must not block others
                _logger.error(
                    "event_handler_failed",
                    event_type=event_type,
                    lane=self.name,
                    error=str(exc),
                )


class RedisEventBus:
    """Durable event bus over a single Redis Stream + per-lane consumer groups."""

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
        supervise_interval_seconds: float = 0.5,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._stream = f"{stream_prefix}:stream"
        self._group = group
        self._consumer = consumer
        self._block_ms = block_ms
        self._batch = batch
        self._stop = asyncio.Event()
        self._max_len = max(1_000, max_len)
        self._lag_warn = max(1, lag_warn_threshold)
        self._lag_interval = max(5.0, lag_check_interval_seconds)
        # How long a delivered-but-unacked message may sit before another
        # consumer takes it over. Comfortably longer than any handler chain,
        # short enough that a crashed process does not strand work for a day.
        self._reclaim_after_ms = int(max(30.0, reclaim_after_seconds) * 1000)
        # How often the supervisor looks for lanes registered after it started.
        # Short, because the gap it covers is startup: `setup()` registers the
        # runtimes a fraction of a second after `run` is spawned, and a lane that
        # waits seconds for its first loop is a lane that starts behind.
        self._supervise_interval = max(0.05, supervise_interval_seconds)
        self._lanes: dict[str, _ConsumerLane] = {}

    @property
    def last_cycle_at(self) -> float | None:
        """Unix time of the *oldest* lane's last completed consume cycle.

        The consumer loop is a bare ``asyncio`` task inside a process whose
        health is measured by a liveness file the *main* coroutine touches. So
        the loop can die while every probe stays green — which is exactly what
        happened.

        The *oldest* rather than the newest, deliberately: with several lanes,
        reporting the freshest would let eleven healthy loops mask one that has
        stopped, which is the same masking this property exists to defeat.

        **Nothing in the health path reads this yet** — only the bus load test
        does. :class:`EventBusConsumerProbe` asks Redis how long each consumer
        group has been idle, which is cross-process and does not depend on a
        service's opinion of itself, but it can only see groups that *exist*: a
        lane whose loop never started never called ``XGROUP CREATE``, so it is
        absent rather than idle, and absent reads as nothing at all. That is the
        gap the 2026-08-06 deploy fell through, and closing it means comparing
        the lanes a process registered against the groups Redis actually holds.
        """
        stamps = [lane.last_cycle_at for lane in self._lanes.values()]
        if not stamps or any(stamp is None for stamp in stamps):
            # A lane that has never turned is not "fresh"; it is unknown, and an
            # unknown must never read as healthy.
            return None
        return min(stamp for stamp in stamps if stamp is not None)

    @property
    def lanes(self) -> tuple[str, ...]:
        return tuple(sorted(self._lanes))

    def group_for(self, lane: str) -> str:
        """The Redis consumer-group name backing ``lane``."""
        return self._group if lane == DEFAULT_LANE else f"{self._group}.{lane}"

    def _lane(self, name: str) -> _ConsumerLane:
        lane = self._lanes.get(name)
        if lane is None:
            lane = _ConsumerLane(
                name=name,
                group=self.group_for(name),
                stream=self._stream,
                consumer=self._consumer,
                provider=self._provider,
                registry=self._registry,
                stop=self._stop,
                block_ms=self._block_ms,
                batch=self._batch,
                lag_warn=self._lag_warn,
                lag_interval=self._lag_interval,
                reclaim_after_ms=self._reclaim_after_ms,
            )
            self._lanes[name] = lane
        return lane

    # --- EventBus interface ---------------------------------------------------
    def subscribe(
        self, event_type: str, handler: EventHandler, *, lane: str = DEFAULT_LANE
    ) -> None:
        self._lane(lane).subscribe(event_type, handler)
        _logger.debug(
            "handler_subscribed", event_type=event_type, group=self.group_for(lane), lane=lane
        )

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
    async def run(self) -> None:
        """Supervise every lane's consumer loop until :meth:`stop`.

        **Supervise, not gather.** The first version of this method took the
        lanes it found and gathered them, which reads as correct and is not:
        ``ServiceProcess.run`` spawns this *before* ``setup()``, so at call time
        the process has registered nothing and the snapshot is empty. Deployed,
        that gave the Worker one default-lane loop with no handlers — reading and
        acking every event without dispatching it — while the twelve lanes
        registered a moment later in ``setup()`` sat with no loop at all. The
        decision path stopped, and because the one loop *was* turning, both the
        liveness probe and the lag metric looked better than before.

        So the bus no longer depends on being started after its subscribers. It
        polls for lanes it has not started yet and starts them, which makes the
        ordering between ``run`` and ``subscribe`` irrelevant in both directions.
        A lane whose loop ends while the bus is still running is restarted and
        reported: ``_ConsumerLane.run`` is written so that nothing but shutdown
        can end it, so a finished task means an assumption broke.

        A process with no subscriptions still gets the default lane, so ``run``
        never returns immediately and leaves a service looking like it consumes
        when it holds no loop at all.
        """
        if not self._lanes:
            self._lane(DEFAULT_LANE)
        tasks: dict[str, asyncio.Task[None]] = {}
        try:
            while not self._stop.is_set():
                self._start_new_lanes(tasks)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._supervise_interval)
                except TimeoutError:
                    continue
        finally:
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)

    def _start_new_lanes(self, tasks: dict[str, asyncio.Task[None]]) -> None:
        """Give a loop to every lane that has none, or has lost the one it had."""
        for name in list(self._lanes):
            existing = tasks.get(name)
            if existing is not None and not existing.done():
                continue
            if existing is not None:
                _logger.error(
                    "redis_bus_lane_restarted",
                    group=self.group_for(name),
                    lane=name,
                    note="a lane loop ended while the bus was still running",
                )
            tasks[name] = asyncio.create_task(self._lanes[name].run(), name=f"bus_lane_{name}")

    def stop(self) -> None:
        self._stop.set()


__all__ = ["DEFAULT_LANE", "RedisEventBus"]
