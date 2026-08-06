"""Event bus — the asynchronous backbone of the platform.

Contexts never call each other directly. They publish domain events; interested
contexts subscribe. This keeps modules decoupled and independently deployable:
today the bus is in-process, tomorrow the same interface is backed by Redis
Streams (or Kafka) so a context can move to its own container without any caller
changing.

The port is :class:`EventBus`. Two implementations ship:
- :class:`InMemoryEventBus` for tests and single-process runs.
- A Redis-backed bus (see ``infrastructure``) selected by ``EVENT_BUS_TRANSPORT``.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.logging import get_logger

# A handler reacts to one event. Handlers must be idempotent: the bus guarantees
# at-least-once delivery, never exactly-once.
EventHandler = Callable[[DomainEvent], Awaitable[None]]

_logger = get_logger("events.bus")


@runtime_checkable
class EventBus(Protocol):
    """Publish/subscribe contract. Route on ``event_type`` (the class name)."""

    def subscribe(self, event_type: str, handler: EventHandler, *, lane: str = "main") -> None:
        """Register ``handler`` for events whose ``event_type`` matches.

        ``lane`` names an independent delivery path. The durable bus gives each
        lane its own consumer group and read loop, so a slow handler chain can
        only delay its own lane; in-process transports deliver concurrently
        already and accept the argument to keep one subscriber API. Order holds
        within a lane and not between lanes, so a lane boundary belongs only
        where the contexts were already coupled through events alone.
        """
        ...

    async def publish(self, event: DomainEvent) -> None:
        """Publish a single event to all matching subscribers."""
        ...

    async def publish_many(self, events: list[DomainEvent]) -> None:
        """Publish a batch, preserving order."""
        ...


class InMemoryEventBus:
    """Simple asyncio in-process bus. Delivers to handlers concurrently.

    Handler exceptions are logged and swallowed so one failing subscriber never
    blocks others — reliability guarantees belong to the durable (Redis) bus.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: EventHandler, *, lane: str = "main") -> None:
        # Lanes are a durability/back-pressure concern, and this bus has neither
        # a consumer group nor a read loop to partition: `publish` already fans
        # out concurrently, which is the isolation lanes buy on the Redis side.
        # Accepting and ignoring the argument keeps one subscriber API across
        # transports instead of making every caller ask which bus it is on.
        self._handlers[event_type].append(handler)
        _logger.debug("handler_subscribed", event_type=event_type, lane=lane)

    async def publish(self, event: DomainEvent) -> None:
        handlers = self._handlers.get(event.event_type, [])
        if not handlers:
            return
        results = await asyncio.gather(
            *(self._safe_invoke(h, event) for h in handlers),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                _logger.error(
                    "event_handler_failed",
                    event_type=event.event_type,
                    error=str(result),
                )

    async def publish_many(self, events: list[DomainEvent]) -> None:
        for event in events:
            await self.publish(event)

    @staticmethod
    async def _safe_invoke(handler: EventHandler, event: DomainEvent) -> None:
        await handler(event)


class LaneBus:
    """An :class:`EventBus` view whose subscriptions all land on one lane.

    The lane a handler belongs to is a *deployment* fact — which read loop should
    be allowed to block which other read loop — and the contexts have no business
    knowing it. There are 47 ``subscribe`` call sites across the contexts, and
    threading a lane argument through all of them would have put that deployment
    fact inside Security, Portfolio and Strategy, where it would then have to be
    kept correct forever.

    So the runtime that *composes* a subsystem wraps the bus once, and everything
    it wires inherits the lane without naming it. Publishing is forwarded
    untouched: lanes partition consumption only, and there is still exactly one
    stream.
    """

    def __init__(self, inner: EventBus, lane: str) -> None:
        self._inner = inner
        self._lane = lane

    @property
    def lane(self) -> str:
        return self._lane

    def subscribe(self, event_type: str, handler: EventHandler, *, lane: str | None = None) -> None:
        # An explicit `lane=` from a caller wins, so a subsystem that genuinely
        # needs to split itself still can; the view supplies the default, it does
        # not overrule.
        self._inner.subscribe(event_type, handler, lane=lane or self._lane)

    async def publish(self, event: DomainEvent) -> None:
        await self._inner.publish(event)

    async def publish_many(self, events: list[DomainEvent]) -> None:
        await self._inner.publish_many(events)
