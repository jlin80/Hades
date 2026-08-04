import { useCallback, useEffect, useState } from "react";
import {
  api,
  type DrawdownState,
  type ExposureState,
  type RiskDecision,
  type RiskStatus,
} from "../api/client";
import { Badge, PageHeader, Panel, Placeholder, Row, StatusDot } from "../ui";

function pct(v: number | undefined): string {
  return v === undefined || Number.isNaN(v) ? "—" : `${v.toFixed(2)}%`;
}

/** A limit and how close the book currently is to it. */
function LimitRow({ label, value, limit }: { label: string; value: number; limit: number }) {
  const ratio = limit > 0 ? Math.min(value / limit, 1) : 0;
  const hot = ratio >= 0.8;
  return (
    <div className="py-1.5">
      <div className="flex items-baseline justify-between text-xs">
        <span className="text-gray-300">{label}</span>
        <span className={hot ? "text-red-400" : "text-gray-100"}>
          {pct(value)} <span className="text-hades-muted">/ {pct(limit)}</span>
        </span>
      </div>
      <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-white/5">
        <div
          className={`h-full rounded-full ${hot ? "bg-red-500/50" : "bg-hades-accent/60"}`}
          style={{ width: `${ratio * 100}%` }}
        />
      </div>
    </div>
  );
}

/** A human-gated control. Every one of these changes whether capital can move,
 *  so none of them fires on a single click. */
function Control({
  label,
  danger,
  busy,
  onRun,
}: {
  label: string;
  danger?: boolean;
  busy: boolean;
  onRun: () => void;
}) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 5000);
    return () => clearTimeout(t);
  }, [armed]);

  const tone = danger
    ? "border-red-500/40 text-red-300 hover:bg-red-500/10"
    : "border-white/10 text-gray-200 hover:bg-white/5";

  return (
    <button
      type="button"
      disabled={busy}
      onClick={() => (armed ? (setArmed(false), onRun()) : setArmed(true))}
      className={`rounded-md border px-3 py-1 text-xs transition-colors disabled:opacity-40 ${
        armed ? "border-amber-400/60 text-amber-300" : tone
      }`}
    >
      {busy ? "…" : armed ? "Confirm?" : label}
    </button>
  );
}

export function RiskScreen() {
  const [status, setStatus] = useState<RiskStatus | null>(null);
  const [drawdown, setDrawdown] = useState<DrawdownState | null>(null);
  const [exposure, setExposure] = useState<ExposureState | null>(null);
  const [decisions, setDecisions] = useState<RiskDecision[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(() => {
    api.riskStatus().then(setStatus).catch(() => setStatus(null));
    api.riskDrawdown().then(setDrawdown).catch(() => undefined);
    api.riskExposure().then(setExposure).catch(() => undefined);
    api.riskDecisions().then((r) => setDecisions(r.decisions)).catch(() => undefined);
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, 10_000);
    return () => clearInterval(timer);
  }, [load]);

  /** Issue a command, then re-read. The API confirms the command was published,
   *  not that the Worker applied it — during a Redis outage those differ, and an
   *  operator resetting a Kill Switch needs the difference to be visible. */
  const run = async (name: string, fn: () => Promise<unknown>) => {
    setBusy(name);
    setNotice(null);
    try {
      await fn();
      setNotice(`${name} issued — verifying the worker applied it…`);
      await new Promise((r) => setTimeout(r, 1500));
      load();
    } catch (err) {
      setNotice(`${name} failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setBusy(null);
    }
  };

  const live = status?.live ?? {};
  const ksLevel = live.kill_switch_level ?? 0;
  const breakerOpen = live.circuit_breaker_open === true;
  const emergency = live.emergency_mode === true;
  const halted = ksLevel > 0 || breakerOpen || emergency;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Risk"
        subtitle="The defence layer — the only component that may approve a trade."
      />

      <div className="flex flex-wrap items-center gap-3">
        <StatusDot status={status?.running ? (halted ? "degraded" : "healthy") : "unknown"} />
        <span className="text-sm text-gray-200">
          {!status?.running
            ? "No snapshot — is the worker running?"
            : halted
              ? "Entries are being withheld"
              : "Accepting entries"}
        </span>
        <Badge tone="neutral">approvals {status?.approvals_total ?? 0}</Badge>
        <Badge tone="neutral">rejections {status?.rejections_total ?? 0}</Badge>
      </div>

      {notice && (
        <div className="rounded-lg border border-hades-accent/30 bg-hades-accent/5 px-4 py-2 text-sm text-gray-300">
          {notice}
        </div>
      )}

      <div className="grid gap-6 md:grid-cols-2">
        {/* Brakes withhold *entries*. They never block an exit — stopping one is
            the single way a brake could destroy capital instead of preserving it. */}
        <Panel title="Brakes">
          <Row
            label="Kill switch"
            value={
              <span className="flex items-center gap-2">
                <Badge tone={ksLevel > 0 ? "danger" : "success"}>
                  {live.kill_switch_label ?? "none"}
                </Badge>
                {ksLevel > 0 && (
                  <Control
                    label="Reset"
                    busy={busy === "Kill switch reset"}
                    onRun={() => run("Kill switch reset", api.resetKillSwitch)}
                  />
                )}
              </span>
            }
            danger={ksLevel > 0}
          />
          {live.kill_switch_reason ? (
            <Row label="Reason" value={live.kill_switch_reason} />
          ) : null}

          <Row
            label="Circuit breaker"
            value={
              <span className="flex items-center gap-2">
                <Badge tone={breakerOpen ? "danger" : "success"}>
                  {breakerOpen ? "OPEN" : "closed"}
                </Badge>
                <Control
                  label={breakerOpen ? "Close" : "Trip"}
                  danger={!breakerOpen}
                  busy={busy === "Circuit breaker"}
                  onRun={() =>
                    run("Circuit breaker", () =>
                      breakerOpen
                        ? api.resetCircuitBreaker()
                        : api.tripCircuitBreaker("manual from dashboard"),
                    )
                  }
                />
              </span>
            }
            danger={breakerOpen}
          />
          {(live.circuit_breaker_reasons ?? []).length > 0 && (
            <Row label="Breaker reasons" value={(live.circuit_breaker_reasons ?? []).join(", ")} />
          )}

          <Row
            label="Emergency mode"
            value={
              <span className="flex items-center gap-2">
                <Badge tone={emergency ? "danger" : "success"}>{emergency ? "ON" : "off"}</Badge>
                <Control
                  label={emergency ? "Exit" : "Enter"}
                  danger={!emergency}
                  busy={busy === "Emergency mode"}
                  onRun={() =>
                    run("Emergency mode", () =>
                      emergency
                        ? api.exitEmergency()
                        : api.enterEmergency("manual from dashboard"),
                    )
                  }
                />
              </span>
            }
            danger={emergency}
          />
          <Row label="Consecutive losses" value={String(live.consecutive_losses ?? 0)} />
          <p className="mt-3 text-xs text-hades-muted">
            Brakes withhold new entries. They never block an exit — an open position can
            always be closed.
          </p>
        </Panel>

        <Panel title="Headroom">
          {drawdown && (
            <>
              <LimitRow
                label="Daily loss"
                value={Math.abs(drawdown.daily_loss_pct)}
                limit={drawdown.limits.daily_pct ?? 0}
              />
              <LimitRow
                label="Drawdown"
                value={Math.abs(drawdown.drawdown_pct)}
                limit={drawdown.limits.monthly_pct ?? 0}
              />
            </>
          )}
          {exposure && (
            <>
              <LimitRow
                label="Portfolio exposure"
                value={exposure.portfolio_exposure_pct}
                limit={exposure.caps.max_portfolio_pct ?? 0}
              />
              <Row
                label="Open positions"
                value={`${exposure.open_positions} / ${exposure.caps.max_concurrent_positions ?? "—"}`}
              />
            </>
          )}
          {!drawdown && !exposure && <Placeholder note="No risk snapshot yet." />}
        </Panel>
      </div>

      {/* The rejections are the useful half: they say why nothing is trading. */}
      <Panel title="Recent decisions">
        {decisions.length === 0 ? (
          <Placeholder note="No risk decisions recorded yet." />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-white/5 text-xs uppercase tracking-wide text-hades-muted">
                  <th className="px-3 py-2 font-medium">Token</th>
                  <th className="px-3 py-2 font-medium">Decision</th>
                  <th className="px-3 py-2 font-medium">Reason</th>
                  <th className="px-3 py-2 font-medium">Size</th>
                  <th className="px-3 py-2 font-medium">When</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-white/5">
                {decisions.map((d, i) => (
                  <tr key={`${d.mint}-${i}`}>
                    <td className="px-3 py-2">
                      <span className="text-gray-100">{d.symbol ?? "—"}</span>{" "}
                      <span className="font-mono text-xs text-hades-muted" title={d.mint}>
                        {d.mint.slice(0, 8)}…
                      </span>
                    </td>
                    <td className="px-3 py-2">
                      <Badge tone={d.decision === "approve" ? "success" : "neutral"}>
                        {d.decision}
                      </Badge>
                    </td>
                    <td className="px-3 py-2 text-hades-muted">
                      {d.reject_reason ?? d.headline ?? "—"}
                    </td>
                    <td className="px-3 py-2 text-hades-muted">
                      {d.notional_usd != null ? `$${d.notional_usd.toFixed(2)}` : "—"}
                    </td>
                    <td className="px-3 py-2 text-hades-muted">
                      {d.at ? new Date(d.at).toLocaleString() : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
