import { useCallback, useEffect, useState } from "react";
import { api, type Health, type Status } from "../api/client";
import { Badge, PageHeader, Panel, Row, StatusDot } from "../ui";

// The screen used to fetch once on mount and swallow every failure. If that
// single call lost the race with a still-booting API — which on a small host it
// reliably does — the panels sat at "…" forever with nothing on screen saying
// why. It now retries on an interval and states plainly when the API is
// unreachable.
const REFRESH_MS = 10_000;

const SERVICES = [
  "postgres", "redis", "api", "dashboard", "engine",
  "watchdog", "scheduler", "worker", "notification",
];

export function SystemScreen() {
  const [status, setStatus] = useState<Status | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [info, setInfo] = useState<Record<string, unknown> | null>(null);
  const [reachable, setReachable] = useState(true);

  const load = useCallback(() => {
    api
      .status()
      .then((s) => {
        setStatus(s);
        setReachable(true);
      })
      .catch(() => setReachable(false));
    api.health().then(setHealth).catch(() => undefined);
    api.info().then(setInfo).catch(() => undefined);
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

  const contexts = (info?.contexts as string[]) ?? [];

  return (
    <div>
      <PageHeader title="System status" subtitle="Runtime posture and platform topology." />

      {!reachable && (
        <div className="mb-6 rounded-lg border border-red-500/30 bg-red-950/30 px-4 py-2 text-sm text-red-300">
          The API is unreachable — retrying every {REFRESH_MS / 1000}s. Values below are
          the last ones seen.
        </div>
      )}

      <div className="grid gap-6 md:grid-cols-2">
        <Panel title="Runtime">
          <Row label="Version" value={status?.version ?? "…"} />
          <Row label="Environment" value={status?.environment ?? "…"} />
          <Row label="Instance" value={status?.instance_id ?? "…"} />
          <Row label="Trading mode" value={status?.trading_mode ?? "…"} />
          <Row
            label="Live enabled"
            value={status ? String(status.is_live) : "…"}
            danger={status?.is_live}
          />
          <Row label="Event bus" value={status?.event_bus_transport ?? "…"} />
        </Panel>

        <Panel title="Aggregate health">
          <div className="mb-3 flex items-center gap-2">
            <StatusDot status={health?.status ?? "unknown"} />
            <span className="text-sm text-gray-200">{health?.status ?? "unknown"}</span>
          </div>
          {(health?.components ?? []).map((c) => (
            <Row
              key={c.name}
              label={c.name}
              value={
                <span className="flex items-center gap-2">
                  <StatusDot status={c.status} /> {c.detail}
                </span>
              }
            />
          ))}
        </Panel>

        <Panel title={`Services (${SERVICES.length})`}>
          <div className="flex flex-wrap gap-2">
            {SERVICES.map((s) => (
              <Badge key={s}>{s}</Badge>
            ))}
          </div>
        </Panel>

        <Panel title={`Bounded contexts (${contexts.length})`}>
          <div className="flex flex-wrap gap-2">
            {contexts.map((c) => (
              <Badge key={c}>{c}</Badge>
            ))}
          </div>
        </Panel>
      </div>
    </div>
  );
}
