"""Cross-process log shipping — how the dashboard sees more than one process.

Hades runs as several processes (``api``, ``worker``, ``engine``, ``scheduler``,
``notification``, ``watchdog``). The ring buffer that feeds the dashboard's
terminal is a **process-local** singleton, so the API could only ever show its own
lines: the Worker — which hosts the entire scanner → security → intelligence →
committee → strategy → risk → execution pipeline — was invisible from the UI.

This module closes that gap with a Redis Stream:

* every process **ships** its records with :class:`LogShipper`;
* the API **tails** the stream with :class:`LogTailer` and merges what it reads
  into its local ring buffer, so the existing WebSocket needs no changes.

Design constraints that shape the implementation:

* **Logging must never block, slow down or fail a caller.** The structlog
  processor runs on whatever thread emitted the record, often inside a hot loop,
  and Redis is async and can be down. So shipping is fire-and-forget: records land
  in a bounded in-memory staging deque, and a background task drains it. When the
  deque is full the *oldest* pending records are dropped — losing old log lines is
  always preferable to stalling the engine or growing memory without bound.
* **Redis is never a system of record.** The stream is capped with ``MAXLEN`` and
  is a live view only, exactly like the ring buffer it feeds. Durable logs remain
  the rotating files.
* **A Redis outage degrades to today's behaviour**, never worse: each process
  keeps its own ring buffer, the tailer retries quietly, and nothing raises.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any, Final

from hades.shared_kernel.cache import RedisProvider

#: The Redis Stream every process writes to and the API reads from.
LOG_STREAM_KEY: Final = "hades:logs"

#: Cap on the stream. Approximate trimming (``~``) lets Redis trim on node
#: boundaries, which is dramatically cheaper and is fine for a live view.
STREAM_MAXLEN: Final = 5000

#: How many records may wait in memory before the oldest are dropped. Small on
#: purpose: if the drain task cannot keep up, the useful signal is the *recent*
#: lines, not a backlog.
STAGING_MAXLEN: Final = 1000

#: Records are shipped in batches; this bounds one drain pass.
_BATCH = 100

_DRAIN_INTERVAL_SECONDS: Final = 0.25
_TAIL_BLOCK_MS: Final = 1000


def _encode(record: dict[str, Any]) -> str:
    """Serialise a record for the stream, never raising on exotic values."""
    return json.dumps(record, default=str)


class LogShipper:
    """Stages records in memory and drains them to Redis in the background.

    Not started automatically: a process opts in during setup. A shipper that was
    never started simply accumulates (and drops) records in its bounded deque,
    which is harmless — that is the state of every unit test.
    """

    def __init__(self, provider: RedisProvider, *, role: str) -> None:
        self._provider = provider
        self._role = role
        self._pending: deque[dict[str, Any]] = deque(maxlen=STAGING_MAXLEN)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.dropped = 0

    def submit(self, record: dict[str, Any]) -> None:
        """Stage one record. Called from the logging processor — never blocks.

        This is the only method on the hot path. It does no I/O, takes no lock
        beyond the deque's own atomicity, and cannot raise on a full buffer.
        """
        if len(self._pending) == self._pending.maxlen:
            self.dropped += 1
        enriched = dict(record)
        # The role is what makes a merged stream readable: without it, a line in
        # the dashboard gives no clue which process produced it.
        enriched.setdefault("role", self._role)
        self._pending.append(enriched)

    async def start(self) -> asyncio.Task[None]:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name=f"log-shipper-{self._role}")
        return self._task

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            await self._drain_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_DRAIN_INTERVAL_SECONDS)
            except TimeoutError:
                continue
        # A final pass so the last lines before a graceful shutdown are not lost.
        await self._drain_once()

    async def _drain_once(self) -> None:
        if not self._pending:
            return
        batch = [self._pending.popleft() for _ in range(min(_BATCH, len(self._pending)))]
        try:
            client = self._provider.client()
            pipe = client.pipeline()
            for record in batch:
                pipe.xadd(
                    LOG_STREAM_KEY,
                    {"record": _encode(record)},
                    maxlen=STREAM_MAXLEN,
                    approximate=True,
                )
            await pipe.execute()
        except Exception:
            # Redis being down must never take a process with it, and must never
            # log (that would recurse straight back into this shipper). The
            # records are dropped and the next pass tries again.
            self.dropped += len(batch)


class LogTailer:
    """Reads the merged stream and hands records to a sink (the ring buffer).

    Starts at the stream's tail rather than replaying history: the dashboard wants
    what is happening now, and the API's own snapshot already covers its recent
    past.
    """

    def __init__(self, provider: RedisProvider, *, own_role: str | None = None) -> None:
        self._provider = provider
        self._own_role = own_role
        self._last_id = "$"
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self, sink: Any) -> asyncio.Task[None]:
        """Begin tailing. ``sink`` needs only an ``append(record)`` method."""
        self._stop.clear()
        self._task = asyncio.create_task(self._run(sink), name="log-tailer")
        return self._task

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self, sink: Any) -> None:
        while not self._stop.is_set():
            try:
                await self._read_once(sink)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Quiet retry: a noisy failure here would flood the very buffer
                # this task exists to fill.
                await asyncio.sleep(1.0)

    async def _read_once(self, sink: Any) -> None:
        client = self._provider.client()
        response = await client.xread({LOG_STREAM_KEY: self._last_id}, count=_BATCH,
                                      block=_TAIL_BLOCK_MS)
        if not response:
            return
        for _stream, entries in response:
            for entry_id, fields in entries:
                self._last_id = entry_id
                record = _decode(fields.get("record"))
                if record is None:
                    continue
                # Our own lines are already in the local buffer; re-appending them
                # would show every API line twice.
                if self._own_role is not None and record.get("role") == self._own_role:
                    continue
                sink.append(record)


def _decode(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, str):
        return None
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None
