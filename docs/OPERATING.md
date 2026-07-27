# Operating Hades — where things run and how to see them

Written after a live debugging session in which the dashboard appeared dead while
the platform was in fact running. Everything here is about *observability*: none
of it changes what the bot decides or how it trades.

## Which process does what

```
api           REST + WebSocket + dashboard backend. No domain loops.
worker        ⭐ THE PIPELINE. scanner → features → security → intelligence →
              committee → strategy → risk → execution → portfolio.
engine        Idle. A reserved slot for splitting the decision loop out of the
              worker later. Runs no domain logic today.
scheduler     Periodic jobs.
notification  Consumes notification events and delivers them to Discord.
watchdog      Health probes + recovery.
migrate       One-shot `alembic upgrade head`, gates every app service.
```

**The single most common mistake is reading `docker compose logs engine` to find
out what the bot is doing.** The engine is idle by design; the worker is where
everything happens. Its log line now says so explicitly.

## Seeing what the platform is doing

### The dashboard terminal shows every process

Each process ships its log lines to a Redis Stream (`hades:logs`, capped, live-view
only); the API tails it and merges the result into the buffer behind
`/ws/terminal`. Before this, the ring buffer was process-local, so the terminal
could only ever display the API's own handful of bootstrap lines — the worker was
invisible.

```bash
LOG_SHIPPING_ENABLED=true    # default
```

Fire-and-forget by design: if Redis is down, each process keeps its own buffer and
the terminal degrades to today's behaviour. Logging never blocks a caller, and a
flood drops the oldest staged lines rather than growing memory.

### Trades are visible as they happen

The execution engine emits one structured line per fill and per close:

```
trade_filled     side=buy mint=… symbol=BONK notional_usd=100.0 price=… slippage_bps=… fees_usd=…
position_closed  mint=… symbol=BONK entry_usd=… exit_usd=… fees_usd=… realized_pnl_usd=… roi_pct=… reason=take_profit
```

`realized_pnl_usd` is net of **both** round-trip frictions (entry fee + exit fee).
These appear in the dashboard terminal, in `trading.log`, and — with Discord
enabled — in your channel.

### Positions are marked and exited by the Position Monitor

The exit half of the lifecycle runs in the **worker**, inside the Execution
runtime. Every `EXECUTION_POSITION_MONITOR_INTERVAL_SECONDS` it prices the open
book in one batched request, publishes a `PositionUpdated` per position (this is
what moves unrealised PnL and therefore equity), and issues a SELL when a level
the Risk Manager already approved is crossed:

```
position_exit_triggered  position_id=… mint=… reason=take_profit entry_price=… mark_price=… notional_usd=…
```

`reason` is one of `take_profit`, `stop_loss` or `trailing_stop`. The envelope
comes from the approval itself and travels on the position's tags — the monitor
decides nothing, it only detects a crossing and executes the exit already
authorised. An exit is deliberately **never** blocked by the kill switch,
circuit breaker or emergency mode: those withhold *entries*, and trapping the
platform in a losing position is the one way a brake could destroy capital.

If the balance never moves and you see no `position_exit_triggered` lines, check
in this order:

1. `EXECUTION_POSITION_MONITOR_ENABLED` and `MARKET_PRICE_ORACLE_ENABLED` are
   both on. The monitor is not built without a price oracle, and the startup
   line says so: `position_monitor_not_built`.
2. `execution_runtime_started … price_oracle=true position_monitor=true`.
3. `price_fetch_failed` in the worker log — the price endpoint is unreachable, so
   nothing can be marked. Positions are held, never blind-sold.
4. The execution status snapshot exposes `positions_monitored`; if it is 0 while
   the portfolio shows open positions, they were opened before the monitor
   started (it learns the book from the `PositionOpened` stream, and a restart
   rebuilds cash and positions from `portfolio_state` but not the monitor's own
   registry).

### The book survives a restart

`portfolio_state` holds the live book — cash, realised PnL, peak equity and the
open positions — as one row per trading mode, written on every recompute and
read back on startup:

```
portfolio_restored  cash_usd=… realized_pnl_usd=… open_positions=… saved_at=…
```

Before this table existed the Portfolio Manager was purely in-memory, so a worker
restart silently reset cash to `PAPER_STARTING_BALANCE_USD`. If the balance keeps
returning to exactly the starting figure, look for a restart loop — and note that
the **configured** starting balance always wins over the stored one, so raising
`PAPER_STARTING_BALANCE_USD` still takes effect.

### `/health` answers what is actually alive

It reports the API, every dependency probe (Postgres, Redis, RPC, ClickHouse) and
every background process's heartbeat:

```bash
curl -s localhost:8000/health | jq '.components'
```

A background process that has never started reads `unknown` ("not running here"),
which is deliberately distinct from `unhealthy` ("running but broken") — they call
for different actions, and `unknown` never drags the aggregate down.

**Liveness vs readiness.** `/health` always answers 200 while the process itself is
alive: it is Docker's restart trigger, and a Redis blip must not recycle a healthy
API and turn a dependency hiccup into an outage. `/ready` is the one that returns
503 when a dependency is down — use it for load-balancer gating.

Deeper checks remain at `/health/preflight` and `/health/production-checklist`.

## Discord notifications

Everything is already built — fills, failed orders, risk events, health, research.
It ships disabled; enabling it is configuration only:

```bash
NOTIFY_DISCORD_ENABLED=true
NOTIFY_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/…
NOTIFY_DISCORD_TRADES_WEBHOOK_URL=…    # optional dedicated channel
NOTIFY_DISCORD_ALERTS_WEBHOOK_URL=…    # optional, warnings + critical
NOTIFY_MIN_SEVERITY=info
```

The `notification` service must be running — it is what consumes the events.

Never commit a webhook URL. `.env` is gitignored; `.env.example` is not, and this
repository is public. A webhook is a write credential for your channel: anyone
holding it can post. If one leaks, delete it in Discord and create a new one.

## Scanner endpoints are configuration

Each adapter ships a working default. To point one at a mirror, a proxy or a
replacement API, no code change is needed:

```bash
SCANNER_SOURCE_URLS={"dexscreener": "https://my-bridge.internal/dex"}
```

The adapter's **parser is unchanged**, so a substitute endpoint must still return
that source's payload shape. If it does not, write an adapter instead and register
it in `SOURCE_REGISTRY`.

### Endpoint status, verified against live hosts on 2026-07-26

| source | status | note |
|---|---|---|
| `dexscreener` | 200 | working |
| `raydium` | 200 | fixed — `poolSortField=created` is not a value v3 accepts |
| `pumpfun` | 200 | fixed — moved to `frontend-api-v3`; the old host answers 530 |
| `orca` | 200 | working |
| `jupiter` | **404** | endpoint moved or retired; override or leave it out |
| `meteora` | **404** | endpoint moved or retired; override or leave it out |

A dead default is worse than an obviously-missing one: the scanner tolerates a
source failing, so a source can be permanently down for months while the platform
reports itself healthy. If you add or change an adapter, curl the endpoint from a
real host — the value is only trustworthy when it was checked, and these will rot
again.

`jupiter` and `meteora` are left pointing at their known-404 URLs on purpose
rather than replaced with a guess: a wrong URL fails in a way that looks like a
transient outage, which is harder to debug than a documented dead one.

## Log lines that look alarming and are not

- **`component_no_recovery_action` (debug).** The Watchdog can only reconnect its
  own Redis/Postgres and reload config. `api`, `dashboard`, `resources` and `rpc`
  have no action wired there, so it reports it has nothing to try and moves on.
  The component being down is still alerted on by the health monitor.
- **`component_recovered component=postgres` on a loop.** The probe is flapping
  (usually a resource-starved host) and each cycle genuinely reconnects. Worth
  investigating as a capacity problem, not a correctness one.

And one that *is* a problem: **`no_notifier_for_channel channel=discord`** means
notifications are being recorded but never delivered — `NOTIFY_DISCORD_ENABLED`
and `NOTIFY_DISCORD_WEBHOOK_URL` are not reaching the `notification` service.
Check its startup line: `discord_disabled` confirms it, `notification_ready
channels=['discord']` means it is wired. It is logged once per channel, not once
per notification.

## Debugging "the bot isn't doing anything"

In order, because each step tells you whether the next one is worth taking:

1. `curl -s localhost:8000/health | jq '.components'` — is `worker` healthy? If it
   reads `unknown`, the pipeline is not running at all.
2. `docker compose logs worker --tail=200` — **not** `engine`.
3. `curl -s localhost:8000/ready` — dependencies answering?
4. Scanner discovering? Look for candidate events in the worker log, and check
   `SCANNER_MIN_LIQUIDITY_USD` is not filtering everything out.
5. Committee predicting but never trading? Check the Risk Manager: it is the only
   component that authorises a trade, it is fail-closed (any exception rejects),
   and it runs Kill Switch / Circuit Breaker / Emergency Mode *before* any
   token-specific logic. `/api/v1/risk` shows its posture. Rejections are
   labelled by the rule that vetoed them — `LOW_SECURITY_SCORE`,
   `LOW_PROBABILITY`, `LOW_CONFIDENCE` — and the facts behind them now come from
   the Security Engine's actual verdict rather than the committee's own
   probabilities, so a `LOW_SECURITY_SCORE` describes the token.
6. `MODELS 0` on the AI page is normal until training produces a candidate — the
   committee runs on documented default priors. Identical probabilities across
   *every* token mean the features are not varying, which is a scanner/feature
   problem, not a committee problem.
7. Trading but the balance never moves? That is the *exit* half, not the entry
   half — see "Positions are marked and exited by the Position Monitor" above.
