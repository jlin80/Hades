# Concurrency inside a lane — design, before any code

**Status:** proposal. Nothing here is implemented. It exists because the two
previous attempts at this problem were deployed and rolled back on the same day,
and the difference between bad luck and a pattern is whether the third attempt
starts from a written argument.

**Author's constraint:** this document must be judged before it is built. If the
reasoning below is wrong, that is much cheaper to discover here.

---

## 1. The ceiling, stated exactly

`RedisEventBus._consume_once` awaits each message in turn, and `_dispatch` awaits
each handler in turn:

```python
for message_id, fields in messages:
    await self._dispatch(fields)      # every handler, sequentially
    await client.xack(...)
```

So for one lane:

    lane throughput  =  1 / (per-event latency)

That is not a tuning constant, it is the shape of the loop. Two measurements from
§6v put numbers on it:

| stage | measured | source |
|---|---|---|
| Security assemble (I/O) | 21.42 s/token (pre-fix) | `hades_security_assemble_seconds` |
| Security analyzers (compute) | 0.010 s/token | `hades_security_analyzers_seconds` |
| Scanner production | ~2.7 ev/s | lane lag growth, 2026-08-06 |

Even at a per-event latency of 0.13 s — the best the assembler fix achieved with
one-step fakes — a single lane tops out near 7.7 ev/s *if nothing else shares the
loop*, and the worker hosts twelve runtimes on 2 vCPU. **Lowering latency raises
the ceiling; only concurrency lifts it off the diagonal.**

## 2. Why the two rolled-back attempts do not count as trying this

Both were topology changes, not concurrency changes:

- `3cd8216` (lanes) gave each subset of handlers its **own consumer group and its
  own loop**. Every group receives a full copy of the stream, so twelve lanes
  multiplied read volume by twelve on a 2-core box — and each lane still
  processed one event at a time.
- `8cfbc09` (reclaim rate-limit) removed a real cost (`XAUTOCLAIM` running 1:1
  with `XREADGROUP`, 167 s of Redis CPU) and lag grew at the same rate afterwards.

The lesson recorded on 2026-08-06 — *"adivinar topologías de bus ya se agotó como
línea"* — is precisely right, and this proposal is not another topology. It
changes what one loop does with a batch it has already read.

## 3. The proposal

Dispatch the messages of a single `XREADGROUP` batch concurrently, bounded, and
ack each message only after its own dispatch resolves.

```python
async def _consume_once(self) -> None:
    ...
    for _stream, messages in response:
        await self._dispatch_batch(messages)     # replaces the for/await/xack

async def _dispatch_batch(self, messages) -> None:
    semaphore = asyncio.Semaphore(self._concurrency)   # 1 == today's behaviour

    async def one(message_id, fields) -> None:
        async with semaphore:
            await self._dispatch(fields)
            await client.xack(self._stream, self._group, message_id)

    await asyncio.gather(*(one(mid, f) for mid, f in messages))
```

`self._concurrency` defaults to **1**, which is byte-for-byte today's semantics.
It is raised per lane, by configuration, and only for lanes whose cost is I/O
wait — Security above all.

### Why per-batch and not a free-running worker pool

A pool decoupled from the read loop would drift: nothing would bound how many
events are in flight, `_check_lag` would report against a moving target, and
shutdown would have to drain a queue nobody owns. Gathering *within* the batch
keeps the cycle boundary intact — one read, one bounded fan-out, one point where
everything is acked or visibly not — so `last_cycle_at`, the lag check and the
stop signal all keep meaning what they mean today.

## 4. What makes this safe, and what does not

**Idempotency is already a contract, and is now actually exercised.** The bus has
always been at-least-once; §6q's orphan reclaim made redelivery routine, and §6u
put persisted `event_id` guards on all three money-mutating handlers. Concurrency
does not introduce redelivery — it was already there.

**But idempotent is not the same as commutative, and that is the real question.**
Two events for the *same token* processed out of order can produce a different
outcome than either order alone. So the fan-out must not be over raw messages:

> **Partition the batch by token mint. Messages sharing a mint run sequentially,
> in stream order; different mints run concurrently.**

This is the whole safety argument, and it is cheap: group the batch by
`event.token.mint` (falling back to "no mint" → its own sequential chain), then
gather over the *groups*. Ordering within a token is preserved exactly as today.

**Ack-on-completion, per message, not per batch.** A batch-level ack after
`gather` would ack messages whose handlers never ran if the process died
mid-batch. Acking inside `one()` keeps the pending list honest: whatever did not
finish stays pending and `XAUTOCLAIM` reclaims it — the mechanism that already
exists.

**`_dispatch` already swallows handler exceptions** (`event_handler_failed`), so
`gather` cannot be poisoned by one bad handler. Do **not** add
`return_exceptions=True` and move on: that would silently convert a
never-should-happen bus error into a lost event.

## 5. What this does not fix, said plainly

- **It does not raise the Scanner's production rate**, and it does not help lanes
  whose cost is CPU. Python's GIL means the ten analyzers (0.010 s, pure compute)
  gain nothing. Security is worth doing because its cost is *waiting*.
- **It does not fix the RPC.** With one provider answering 401 and no free spare
  able to serve `getTokenLargestAccounts`, concurrency would parallelise
  failures. **This must not be deployed while the platform is blind** — the
  measurement afterwards would be meaningless, which is exactly the trap of the
  first lanes deploy, where a lag falling to 1,982 meant the opposite of progress.
- **It does not remove the per-provider rate limit.** `RpcManager` throttles per
  provider, so raising lane concurrency without raising the provider budget just
  moves the queue. Expect the bound to be set by the RPC plan, not by the CPU.

## 6. Tests that must fail against the current code

1. **Ordering within a mint.** Two events for one mint, the first handler slow;
   assert the second handler observes the first's effect. Fails today only if
   written against the *new* signature — so instead: assert on **observed order**
   with a shared list, which the sequential version also satisfies. This test is
   a guard, not a proof of the fix; say so in its docstring.
2. **Concurrency across mints** (the one that proves the change). Ten mints, each
   handler sleeping 0.1 s, concurrency 5: assert wall-clock < 0.5 s. Against
   today's loop it takes ~1 s and **fails on elapsed time**, not on a signature —
   the standard this project set in `6778fa1`.
3. **A failed dispatch leaves its message unacked** while its batch peers are
   acked. Kill one handler mid-batch; assert the pending entry survives.
4. **`concurrency=1` is byte-for-byte today.** Same order, same acks, same
   number of `XACK` calls.
5. **Shutdown drains the in-flight batch** rather than abandoning it.

## 7. Rollout, given the history

1. Merge with `concurrency=1` everywhere. **This is a no-op deploy** and should
   be verified as one: same lag slope, same throughput, before anything is raised.
2. Only after the RPC is healthy, raise **Security alone** to 4 and compare
   `hades_security_assemble_seconds` count/rate over a window of at least 10
   minutes, against a baseline taken in the same system state.
3. Fixed criterion, written before the deploy: **if lane lag slope does not
   improve, roll back.** Not "investigate further" — roll back, then investigate.
4. Do not raise a second lane in the same deploy. Two variables, one measurement,
   is how the last two attempts became unattributable.

## 8. Open question I cannot answer from the code

Whether any handler holds cross-token state that the mint partition does not
protect — a shared cache written during dispatch, a counter, a `last_seen`. The
Risk facts cache (`OrderedDict`) and the exploration TTL cache are the obvious
candidates. **This must be audited before step 2**, and if such state exists the
answer is to make it safe, not to widen the partition.

---

*Related: §6v (metering and the two wrong attributions), §6u (idempotency and the
book fix), `docs/BUS_LOAD_2026-08-04.md` (the load test that measured the bus, not
this path).*
