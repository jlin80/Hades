import { useEffect, useState } from "react";
import { api, type AnomalyRow, type ScannerStatus } from "../api/client";
import { Badge, PageHeader, Panel, Row, StatusDot } from "../ui";

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-lg border border-white/5 bg-black/20 p-4">
      <div className="text-xs uppercase tracking-wide text-hades-muted">{label}</div>
      <div className="mt-1 text-2xl font-semibold text-gray-100">{value}</div>
    </div>
  );
}

export function ScannerScreen() {
  const [status, setStatus] = useState<ScannerStatus | null>(null);
  const [anomalies, setAnomalies] = useState<AnomalyRow[]>([]);
  const [byKind, setByKind] = useState<Record<string, number>>({});

  useEffect(() => {
    const load = () => {
      api.scannerStatus().then(setStatus).catch(() => undefined);
      api
        .scannerAnomalies()
        .then((r) => {
          setAnomalies(r.anomalies);
          setByKind(r.by_kind);
        })
        .catch(() => undefined);
    };
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, []);

  const live = status?.live ?? {};
  const rpcEndpoints = live.rpc_endpoints ?? [];
  const sources = live.sources ?? {};

  return (
    <div className="space-y-6">
      <PageHeader title="Scanner" subtitle="Data acquisition — discovery only, no risk or scoring." />

      <div className="flex items-center gap-2">
        <StatusDot status={status?.running ? "healthy" : status?.enabled ? "degraded" : "unknown"} />
        <span className="text-sm text-gray-200">
          {status?.running ? "Running" : status?.enabled ? "Enabled (starting)" : "Disabled"}
        </span>
        {live.rpc_active && <Badge tone="success">RPC: {live.rpc_active}</Badge>}
      </div>

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <Stat label="Tokens analysed" value={status?.tokens_total ?? 0} />
        <Stat label="New (1h)" value={status?.tokens_active_1h ?? 0} />
        <Stat label="Features / sec" value={live.features_per_second ?? 0} />
        <Stat label="Queue depth" value={live.queue_depth ?? 0} />
        <Stat label="Active workers" value={`${live.active_workers ?? 0} / ${live.worker_count ?? 0}`} />
        <Stat label="DEX active" value={`${live.sources_available ?? 0} / ${live.sources_total ?? 0}`} />
        <Stat label="Features computed" value={live.features_total ?? 0} />
        <Stat label="Anomalies" value={status?.anomalies_total ?? 0} />
      </div>

      <Panel title="Discovery sources">
        {Object.keys(sources).length === 0 && (
          <p className="text-sm text-hades-muted">
            {(status?.configured_sources ?? []).join(", ") || "No sources configured."}
          </p>
        )}
        {Object.entries(sources).map(([name, up]) => (
          <Row
            key={name}
            label={name}
            value={
              <span className="flex items-center gap-2">
                <StatusDot status={up ? "healthy" : "unhealthy"} /> {up ? "available" : "down"}
              </span>
            }
          />
        ))}
      </Panel>

      <Panel title="RPC providers">
        {rpcEndpoints.length === 0 && <p className="text-sm text-hades-muted">No RPC data yet.</p>}
        {rpcEndpoints.map((e) => (
          <Row
            key={e.name}
            label={e.name}
            value={
              <span className="flex items-center gap-2">
                <StatusDot status={e.status} />
                {e.name === live.rpc_active && <Badge tone="success">active</Badge>}
                {e.latency_ms.toFixed(0)} ms · health {e.health_score.toFixed(0)} · {e.total_failures} fails
              </span>
            }
          />
        ))}
      </Panel>

      {/* A total on its own is not actionable: 420 malformed payloads from one
          broken source and 420 tokens missing a social link call for completely
          different responses. The breakdown is what makes the number mean something. */}
      <Panel title="Data-quality anomalies">
        {Object.keys(byKind).length === 0 && (
          <p className="text-sm text-hades-muted">No anomalies recorded.</p>
        )}
        <div className="mb-3 flex flex-wrap gap-2">
          {Object.entries(byKind).map(([kind, count]) => (
            <Badge key={kind} tone={kind === "malformed" ? "danger" : "neutral"}>
              {kind}: {count}
            </Badge>
          ))}
        </div>
        {anomalies.slice(0, 15).map((a, i) => (
          <Row
            key={`${a.subject}-${a.kind}-${a.field}-${i}`}
            label={
              <span className="font-mono text-xs">
                {a.subject.slice(0, 10)}… · {a.field}
              </span>
            }
            value={
              <span className="text-xs">
                <span className="text-hades-muted">{a.kind}</span> — {a.detail}
                {/* A problem seen once and a problem seen 900 times read
                    identically without this; the count is the whole diagnosis. */}
                {a.occurrences > 1 && (
                  <span className="ml-2 text-hades-muted">×{a.occurrences}</span>
                )}
              </span>
            }
          />
        ))}
      </Panel>
    </div>
  );
}
