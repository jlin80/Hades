import { useCallback, useEffect, useState } from "react";
import { api, type Health } from "../api/client";
import { PageHeader, Panel, Row, StatusDot } from "../ui";

const REFRESH_MS = 10_000;

/**
 * The Health screen used to fetch once on mount and swallow the failure:
 * `api.health().then(setHealth).catch(() => undefined)` with an empty dependency
 * array. If that single call failed, `health` stayed null forever and the panel
 * showed "Loading…" for the rest of the session — and if it succeeded, the
 * operator kept looking at a snapshot from page load no matter what happened
 * afterwards.
 *
 * Both halves matter more here than on any other screen, because this is the one
 * whose entire job is to say that something is wrong. Every other screen already
 * polls; this was the outlier.
 */
export function HealthScreen() {
  const [health, setHealth] = useState<Health | null>(null);
  const [reachable, setReachable] = useState(true);
  const [checkedAt, setCheckedAt] = useState<Date | null>(null);

  const load = useCallback(() => {
    api
      .health()
      .then((h) => {
        setHealth(h);
        setReachable(true);
        setCheckedAt(new Date());
      })
      .catch(() => setReachable(false));
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

  return (
    <div>
      <PageHeader
        title="Health"
        subtitle="Dependency and resource probes (driven by the Watchdog)."
      />

      {!reachable && (
        <div className="mb-6 rounded-lg border border-red-500/30 bg-red-950/30 px-4 py-2 text-sm text-red-300">
          The API is unreachable — retrying every {REFRESH_MS / 1000}s. The health shown below
          {checkedAt ? ` is from ${checkedAt.toLocaleTimeString()}` : " has never been read"}, not
          current. An unreachable API is itself a fault, not a quiet screen.
        </div>
      )}

      <Panel title="Components">
        <div className="mb-3 flex items-center gap-2">
          <StatusDot status={health?.status ?? "unknown"} />
          <span className="text-sm text-gray-200">Overall: {health?.status ?? "unknown"}</span>
          {checkedAt && (
            <span className="text-xs text-hades-muted">
              · checked {checkedAt.toLocaleTimeString()}
            </span>
          )}
        </div>
        {(health?.components ?? []).map((c) => (
          <Row
            key={c.name}
            label={c.name}
            value={
              <span className="flex items-center gap-2">
                <StatusDot status={c.status} /> {c.status} · {c.detail}
              </span>
            }
          />
        ))}
        {!health && reachable && <p className="text-sm text-hades-muted">Loading…</p>}
        {!health && !reachable && (
          <p className="text-sm text-red-300">
            No health has been read yet — the API has not answered since this screen opened.
          </p>
        )}
      </Panel>
    </div>
  );
}
