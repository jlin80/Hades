"""Decision Engine process — a supervised placeholder, and honest about it.

The trading pipeline does **not** live here. Scanner, Security Engine, Wallet
Intelligence, AI Committee, Strategy Engine, Risk Manager and Execution Engine are
all hosted by the :class:`~hades.ops.worker.Worker` process, which is where the
flow ``scanner → features → security → intelligence → committee → strategy → risk
→ execution`` actually runs.

This process stays in the topology as a supervised, heartbeating slot reserved for
splitting the decision loop out of the Worker when throughput demands it. It runs
no domain logic today.

That distinction used to be invisible: this process logged
``"no strategies/scanner/AI in Phase 2"``, which reads like a statement about the
*platform* rather than about this one process. An operator running
``docker compose logs engine`` to find out what the bot was doing would conclude
the whole system was an unfinished skeleton, while the Worker was running the
entire pipeline next door. The log now says where to look.
"""

from __future__ import annotations

from hades.ops.service import ServiceProcess


class Engine(ServiceProcess):
    """A supervised, idle process. The pipeline runs in the Worker."""

    role = "engine"

    async def setup(self) -> None:
        self._log.info(
            "engine_ready",
            trading_mode=self._container.settings.trading_mode,
            is_live=self._container.settings.is_live,
            hosts_pipeline=False,
            pipeline_process="worker",
            note=(
                "idle by design — scanner/security/intelligence/committee/strategy/"
                "risk/execution all run in the worker process; inspect its logs to "
                "see trading activity"
            ),
        )
