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
            self._tasks.append(asyncio.create_task(bus.run()))

        await self.setup()
        self._tasks.append(asyncio.create_task(self._liveness_loop()))

        await self._stop.wait()
        await self._shutdown()

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
