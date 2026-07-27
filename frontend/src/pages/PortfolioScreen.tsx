import { useEffect, useState } from "react";
import {
  api,
  type EquityPoint,
  type ExecutionMetrics,
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
        </Panel>
      </div>

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
