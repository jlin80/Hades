"""Audit runtime — wires the append-only audit trail into the Worker.

Builds the store (Postgres when a database is present, in-memory otherwise), the
recorder and the event subscriber, and subscribes the latter to the bus so every
consequential event the platform publishes is recorded. There is no background
loop — auditing is purely event-driven — so :meth:`start` returns no tasks.

The recorder is exposed so other runtimes/services (e.g. the Configuration
Manager) can record directly through the :class:`AuditTrail` port.
"""

from __future__ import annotations

import asyncio

from hades.bootstrap import Container
from hades.contexts.audit.application.recorder import AuditRecorder
from hades.contexts.audit.application.subscriber import AuditSubscriber
from hades.contexts.audit.domain.ports import AuditStore
from hades.contexts.audit.infrastructure.store import InMemoryAuditStore, PostgresAuditStore
from hades.shared_kernel.events.bus import LaneBus
from hades.shared_kernel.logging import get_logger

_logger = get_logger("audit.runtime")


class AuditRuntime:
    """Owns the audit store + recorder + subscriber for the process."""

    def __init__(self, container: Container) -> None:
        self._c = container
        self._bus = LaneBus(container.event_bus, "audit")
        self._store: AuditStore = (
            PostgresAuditStore(container.database)
            if container.database is not None
            else InMemoryAuditStore()
        )
        self._recorder = AuditRecorder(self._store)
        self._subscriber = AuditSubscriber(self._recorder)

    @property
    def recorder(self) -> AuditRecorder:
        return self._recorder

    async def start(self) -> list[asyncio.Task[None]]:
        self._subscriber.register(self._bus)
        _logger.info("audit_runtime_started", backend=type(self._store).__name__)
        return []

    async def stop(self) -> None:
        _logger.info("audit_runtime_stopped")
