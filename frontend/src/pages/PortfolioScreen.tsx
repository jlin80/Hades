import { useEffect, useState } from "react";
import {
  api,
  type EquityPoint,
  type ExecutionMetrics,
  type Funnel,
  type FunnelStage,
  type OpenPosition,
  type PnlRow,
  type PortfolioStatus,
} from "../api/client";
import { Badge, PageHeader, Panel, Placeholder, Row, StatusDot } from "../ui";

function usd(value: number | undefined): string {
  if (value === undefined || Number.isNaN(value)) return "—";
  return `$${value.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function pct(value: number | undefined): string {
  if (value === undefined || Number.isNaN(value)) return "—";
  return `${value.toFixed(2)}%`;
}

/** A figure whose sign is the point: green up, red down, neutral at zero. */
function Signed({
  label,
  value,
  format,
}: {
  label: string;
  value?: number;
  format: "usd" | "pct";
}) {
  const n = value ?? 0;
  const tone = n > 0 ? "text-emerald-400" : n < 0 ? "text-red-400" : "text-gray-100";
  return (
    <div className="rounded-lg border border-white/5 bg-black/20 p-4">
      <div className="text-xs uppercase tracking-wide text-hades-muted">{label}</div>
      <div className={`mt-1 text-2xl font-semibold ${tone}`}>
        {format === "usd" ? usd(value) : pct(value)}
      </div>
    </div>
  );
}

function Stat({ label, value, hint }: { label: string; value: string | number; hint?: string }) {
  return (
    <div className="rounded-lg border border-white/5 bg-black/20 p-4">
      <div className="text-xs uppercase tracking-wide text-hades-muted">{label}</div>
      <div className="mt-1 text-2xl font-semibold text-gray-100">{value}</div>
      {hint && <div className="mt-1 text-xs text-hades-muted">{hint}</div>}
    </div>
  );
}

/** One funnel stage, its bar scaled against the widest stage (discovery). A
 *  stage at zero is drawn red, because that is the one worth looking at. */
function FunnelBar({ stage, of }: { stage: FunnelStage; of: number }) {
  const width = of > 0 ? Math.max((stage.count / of) * 100, stage.count > 0 ? 1.5 : 0) : 0;
  const dead = stage.count === 0;
  return (
    <div className="py-1.5">
      <div className="flex items-baseline justify-between text-xs">
        <span className={dead ? "text-red-400" : "text-gray-300"}>{stage.label}</span>
        <span className={dead ? "font-medium text-red-400" : "text-gray-100"}>{stage.count}</span>
      </div>
      <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-white/5">
        <div
          className={`h-full rounded-full ${dead ? "bg-red-500/40" : "bg-hades-accent/60"}`}
          style={{ width: `${width}%` }}
        />
      </div>
    </div>
  );
}

/** Equity over time, drawn inline. No chart library: the CSP forbids external
 *  scripts, and a polyline is all this needs. */
function Sparkline({ points }: { points: EquityPoint[] }) {
  if (points.length < 2) return <Placeholder note="Not enough samples yet." />;
  const values = points.map((p) => p.equity_usd);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const path = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * 100;
      const y = 100 - ((v - min) / span) * 100;
      return `${i === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");
  const rising = values[values.length - 1] >= values[0];

  return (
    <div>
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="h-32 w-full">
        <path
          d={path}
          fill="none"
          stroke={rising ? "#34d399" : "#f87171"}
          strokeWidth={1.5}
          vectorEffect="non-scaling-stroke"
        />
      </svg>
      <div className="mt-2 flex justify-between text-xs text-hades-muted">
        <span>{usd(min)}</span>
        <span>{points.length} samples</span>
        <span>{usd(max)}</span>
      </div>
    </div>
  );
}

export function PortfolioScreen() {
  const [status, setStatus] = useState<PortfolioStatus | null>(null);
  const [pnl, setPnl] = useState<PnlRow[]>([]);
  const [curve, setCurve] = useState<EquityPoint[]>([]);
  const [execution, setExecution] = useState<ExecutionMetrics | null>(null);
  const [funnel, setFunnel] = useState<Funnel | null>(null);
  const [positions, setPositions] = useState<OpenPosition[]>([]);

  useEffect(() => {
    const load = () => {
      api
        .portfolio()
        .then(setStatus)
        .catch(() => undefined);
      api
        .portfolioPnl()
        .then((r) => setPnl(r.pnl))
        .catch(() => undefined);
      api
        .portfolioEquityCurve()
        .then((r) => setCurve(r.points))
        .catch(() => undefined);
      api
        .executionMetrics()
        .then(setExecution)
        .catch(() => undefined);
      api
        .funnel()
        .then(setFunnel)
        .catch(() => undefined);
      api
        .portfolioPositions()
        .then((r) => setPositions(r.positions))
        .catch(() => undefined);
    };
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, []);

  const live = status?.live ?? {};
  const stats = execution?.transaction_stats ?? {};
  const counts = execution?.counts ?? {};

  return (
    <div className="space-y-6">
      <PageHeader
        title="Portfolio"
        subtitle="Simulated capital, PnL and the frictions charged on every fill."
      />

      <div className="flex items-center gap-2">
        <StatusDot status={status?.running ? "healthy" : "unknown"} />
        <span className="text-sm text-gray-200">
          {status?.running ? "Tracking" : "No snapshot — is the worker running?"}
        </span>
        <Badge tone={execution?.is_live ? "danger" : "success"}>{execution?.mode ?? "paper"}</Badge>
      </div>

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <Stat
          label="Equity"
          value={usd(live.equity_usd)}
          hint={`start ${usd(live.starting_balance_usd)}`}
        />
        <Stat label="Cash" value={usd(live.cash_usd)} />
        <Stat label="Invested" value={usd(live.invested_usd)} />
        <Stat label="Open positions" value={live.open_positions ?? 0} />
        <Signed label="Realised PnL" value={live.realized_pnl_usd} format="usd" />
        <Signed label="Unrealised PnL" value={live.unrealized_pnl_usd} format="usd" />
        <Signed label="ROI" value={live.roi_pct} format="pct" />
        <Stat
          label="Drawdown"
          value={pct(live.drawdown_pct)}
          hint={`exposure ${pct(live.exposure_pct)}`}
        />
      </div>

      {/* "Invested $57.92" says capital left cash; this says where it went. The
          count on its own is the one number an operator cannot act on. */}
      <Panel title="Open positions">
        {positions.length === 0 ? (
          <Placeholder
            note={
              (live.open_positions ?? 0) > 0
                ? "The book reports open positions but the worker has not published them yet."
                : "No open positions."
            }
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-white/5 text-xs uppercase tracking-wide text-hades-muted">
                  <th className="px-3 py-2 font-medium">Token</th>
                  <th className="px-3 py-2 font-medium">Size</th>
                  <th className="px-3 py-2 font-medium">Unrealised</th>
                  <th className="px-3 py-2 font-medium">Strategy</th>
                  <th className="px-3 py-2 font-medium">Opened</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-white/5">
                {positions.map((p) => (
                  <tr key={p.mint}>
                    <td className="px-3 py-2">
                      <span className="text-gray-100">{p.symbol ?? p.name ?? "—"}</span>{" "}
                      <span
                        className="font-mono text-xs text-hades-muted"
                        title={p.mint}
                      >
                        {p.mint.slice(0, 8)}…
                      </span>
                    </td>
                    <td className="px-3 py-2 text-gray-100">{usd(p.notional_usd)}</td>
                    <td
                      className={`px-3 py-2 ${
                        p.unrealized_pnl_usd > 0
                          ? "text-emerald-400"
                          : p.unrealized_pnl_usd < 0
                            ? "text-red-400"
                            : "text-gray-300"
                      }`}
                    >
                      {usd(p.unrealized_pnl_usd)}
                    </td>
                    <td className="px-3 py-2 text-hades-muted">{p.strategy}</td>
                    <td className="px-3 py-2 text-hades-muted">
                      {p.opened_at ? new Date(p.opened_at).toLocaleString() : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <div className="grid gap-6 md:grid-cols-2">
        <Panel title="Equity curve">
          <Sparkline points={curve} />
        </Panel>

        {/* The frictions are the whole point of paper trading: a simulation that
            ignores them reports profits the real market would never have paid. */}
        <Panel title="Execution frictions">
          <Row label="Fills" value={String(counts.filled ?? 0)} />
          <Row label="Failed orders" value={String(counts.failed ?? 0)} />
          <Row label="Avg slippage" value={`${(stats.avg_slippage_bps ?? 0).toFixed(1)} bps`} />
          <Row label="Total fees" value={usd(stats.total_fees_usd)} />
          <Row
            label="Avg confirmation"
            value={`${(stats.avg_confirmation_ms ?? 0).toFixed(0)} ms`}
          />
          <p className="mt-3 text-xs text-hades-muted">
            Realised PnL is already net of both round-trip frictions — the entry fee
            captured when the position opened plus the exit fee.
          </p>
          {/* These counters live in the worker's memory, while the book is now
              persisted. After a restart they read zero next to positions that
              are genuinely open — which looks like a contradiction until you
              know the counters are session-scoped and the book is not. */}
          <p className="mt-2 text-xs text-hades-muted/70">
            Counted since the worker last started, not since inception — they reset on
            restart while the open book survives it.
          </p>
        </Panel>
      </div>

      {/* "The bot runs but the portfolio never moves" is the hardest question to
          answer from this screen, because a flat equity curve looks identical
          whether nothing qualified or a stage is silently dropping everything.
          The funnel makes the cliff visible: the stage where the bar collapses
          is the stage to go look at. */}
      <Panel title="Pipeline funnel — last 24h">
        {funnel === null ? (
          <Placeholder note="Funnel unavailable." />
        ) : (
          <>
            <p className="mb-4 text-sm text-gray-300">{funnel.diagnosis}</p>
            {funnel.stages.map((stage) => (
              <FunnelBar key={stage.key} stage={stage} of={funnel.stages[0]?.count ?? 0} />
            ))}
            {Object.keys(funnel.reject_reasons).length > 0 && (
              <div className="mt-4 border-t border-white/5 pt-3">
                <div className="mb-2 text-xs uppercase tracking-wide text-hades-muted">
                  Why Risk rejected
                </div>
                <div className="flex flex-wrap gap-2">
                  {Object.entries(funnel.reject_reasons).map(([reason, count]) => (
                    <Badge key={reason} tone="neutral">
                      {reason}: {count}
                    </Badge>
                  ))}
                </div>
              </div>
            )}
          </>
        )}
      </Panel>

      <Panel title="PnL log">
        {pnl.length === 0 && <Placeholder note="No closed trades yet." />}
        {pnl.map((row, i) => (
          <Row
            key={`${row.at}-${i}`}
            label={`${row.at?.replace("T", " ").slice(0, 19) ?? "—"} · ${row.kind}`}
            value={
              <span className={row.amount_usd >= 0 ? "text-emerald-400" : "text-red-400"}>
                {usd(row.amount_usd)}
              </span>
            }
          />
        ))}
      </Panel>
    </div>
  );
}
