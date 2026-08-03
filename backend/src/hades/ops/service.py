"""Base class for long-running background service processes.

Worker, Engine, Scheduler, Watchdog and Notification are all supervised
processes with the same lifecycle: build the container, install signal handlers,
register their tasks, touch a liveness file on a heartbeat, and shut down
gracefully. This base captures that so each concrete service only implements
``setup`` (register its tasks) — matching the ``worker`` skeleton from Phase 1.

If the event bus is the Redis transport, the base also starts its consumer loop
so the service actually receives cross-service events.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Coroutine
from typing import Any

from hades.bootstrap import Container, build_container
from hades.ops.liveness import Liveness
from hades.shared_kernel.events import RedisEventBus
from hades.shared_kernel.logging import get_logger
from hades.shared_kernel.logging.setup import set_log_shipper
from hades.shared_kernel.logging.shipper import LogShipper


class ServiceProcess:
    """A supervised asyncio service with liveness heartbeats + graceful stop."""

    #: Overridden by subclasses; also names the liveness file and log domain.
    role: str = "service"

    def __init__(self, container: Container | None = None) -> None:
        self._container = container or build_container(role=self.role)
        self._log = get_logger(self.role)
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._liveness = Liveness(
            self.role, directory=self._container.settings.watchdog.liveness_dir
        )
        self._shipper: LogShipper | None = None

    @property
    def container(self) -> Container:
        return self._container

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            # add_signal_handler is unavailable on some hosts (e.g. Windows dev).
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._stop.set)

    async def run(self) -> None:
        self._install_signal_handlers()
        self._log.info(
            "service_started", role=self.role, instance_id=self._container.settings.instance_id
        )
        self._start_metrics_server()
        await self._start_log_shipping()

        # If we're on the Redis transport, run the consumer loop for this service.
        bus = self._container.event_bus
        if isinstance(bus, RedisEventBus):
            self._spawn(bus.run(), name="event_bus_consumer")

        await self.setup()
        self._spawn(self._liveness_loop(), name="liveness")

        await self._stop.wait()
        await self._shutdown()

    def _spawn(self, coro: Coroutine[Any, Any, None], *, name: str) -> asyncio.Task[None]:
        """Start a background task that cannot die quietly.

        ``asyncio`` only reports an unretrieved task exception from the task's
        ``__del__``, so a task kept alive in ``self._tasks`` — as every task here
        is — can raise and leave no trace anywhere. That is not hypothetical: the
        Worker's event-bus consumer stopped after thirty minutes and the platform
        ran four more days believing it was consuming, because the exception that
        ended it was never retrieved and never collected.

        Every background task now carries a completion callback. A task that ends
        while the service is still running is a defect by definition — these
        loops are meant to run until :meth:`stop` — so it is logged as an error
        with its traceback, named, and never silent again.
        """
        task = asyncio.create_task(coro, name=f"{self.role}:{name}")
        task.add_done_callback(lambda t: self._on_task_done(name, t))
        self._tasks.append(task)
        return task

    def _on_task_done(self, name: str, task: asyncio.Task[None]) -> None:
        if task.cancelled() or self._stop.is_set():
            return  # ordinary shutdown
        exc = task.exception()
        if exc is None:
            self._log.error(
                "background_task_exited",
                task=name,
                note="a task that should run until shutdown returned on its own",
            )
            return
        self._log.error(
            "background_task_crashed",
            task=name,
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=exc,
        )

    async def _start_log_shipping(self) -> None:
        """Ship this process's log lines so the dashboard terminal can show them.

        Without this the API can only ever display its own lines, which made the
        Worker — where the whole trading pipeline lives — invisible from the UI.
        """
        if not self._container.settings.observability.log_shipping_enabled:
            return
        self._shipper = LogShipper(self._container.redis, role=self.role)
        self._tasks.append(await self._shipper.start())
        set_log_shipper(self._shipper)
        self._log.info("log_shipping_started", role=self.role)

    def _start_metrics_server(self) -> None:
        """Expose this process's metrics for Prometheus (background services have
        no HTTP surface otherwise; the API serves its own ``/metrics``)."""
        obs = self._container.settings.observability
        if not obs.metrics_enabled:
            return
        try:
            from prometheus_client import start_http_server

            start_http_server(obs.metrics_port, registry=self._container.metrics.registry)
            self._log.info("metrics_server_started", port=obs.metrics_port)
        except OSError as exc:
            self._log.warning("metrics_server_failed", error=str(exc))

    async def setup(self) -> None:
        """Register background tasks. Override in subclasses."""

    async def teardown(self) -> None:
        """Release resources started in :meth:`setup`. Override in subclasses.

        Called once on shutdown *before* the background tasks are cancelled, so a
        subclass can stop its subsystems gracefully. The base does nothing.
        """

    async def _liveness_loop(self) -> None:
        interval = self._container.settings.watchdog.heartbeat_interval_seconds
        while not self._stop.is_set():
            self._liveness.touch()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                continue

    async def _shutdown(self) -> None:
        self._log.info("service_stopping", role=self.role)
        try:
            await self.teardown()
        except Exception as exc:  # teardown must not block a clean shutdown
            self._log.error("service_teardown_failed", role=self.role, error=str(exc))
        bus = self._container.event_bus
        if isinstance(bus, RedisEventBus):
            bus.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._container.shutdown()
        self._log.info("service_stopped", role=self.role)
