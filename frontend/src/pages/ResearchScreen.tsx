// The Research screen used to be a placeholder reading "The Research Lab is not
// built in this phase" — which was false: the whole `/api/v1/research` surface
// exists and is populated. This wires the screen to it.
//
// Everything here is read-only with one exception: recording a promotion
// decision. Even that deploys nothing — the API is explicit that it stores a
// governance decision and activates no strategy and no live trading. The screen
// says so rather than letting the button imply more than it does.

import { useCallback, useEffect, useState } from "react";
import {
  api,
  type ResearchBacktest,
  type ResearchCandidate,
  type ResearchExperiment,
  type ResearchKnowledgeEntry,
  type ResearchPromotion,
  type ResearchReport,
  type ResearchStatus,
  type ShadowStrategy,
} from "../api/client";
import { Tabs } from "../components/Tabs";
import { Badge, PageHeader, Panel, Placeholder, StatusDot } from "../ui";

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-lg border border-white/5 bg-black/20 p-4">
      <div className="text-xs uppercase tracking-wide text-hades-muted">{label}</div>
      <div className="mt-1 text-2xl font-semibold text-gray-100">{value}</div>
    </div>
  );
}

function num(value: number | null | undefined, digits = 2, suffix = ""): string {
  return value === null || value === undefined ? "—" : `${value.toFixed(digits)}${suffix}`;
}

function when(at: string | null): string {
  return at ? new Date(at).toLocaleString() : "—";
}

function Empty({ what }: { what: string }) {
  return <Placeholder note={`No ${what} recorded yet.`} />;
}

/** Shared table shell — the lab's views are all "recent rows of X". */
function Table({ head, children }: { head: string[]; children: React.ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="border-b border-white/5 text-xs uppercase tracking-wide text-hades-muted">
            {head.map((h) => (
              <th key={h} className="px-3 py-2 font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-white/5">{children}</tbody>
      </table>
    </div>
  );
}

export function ResearchScreen() {
  const [status, setStatus] = useState<ResearchStatus | null>(null);
  const [experiments, setExperiments] = useState<ResearchExperiment[]>([]);
  const [backtests, setBacktests] = useState<ResearchBacktest[]>([]);
  const [candidates, setCandidates] = useState<ResearchCandidate[]>([]);
  const [shadows, setShadows] = useState<ShadowStrategy[]>([]);
  const [promotions, setPromotions] = useState<ResearchPromotion[]>([]);
  const [knowledge, setKnowledge] = useState<ResearchKnowledgeEntry[]>([]);
  const [reports, setReports] = useState<ResearchReport[]>([]);
  const [reachable, setReachable] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .researchStatus()
      .then((s) => {
        setStatus(s);
        setReachable(true);
      })
      .catch(() => setReachable(false));
    api.researchExperiments().then((r) => setExperiments(r.experiments)).catch(() => undefined);
    api.researchBacktests().then((r) => setBacktests(r.backtests)).catch(() => undefined);
    api.researchCandidates().then((r) => setCandidates(r.candidates)).catch(() => undefined);
    api.researchShadows().then((r) => setShadows(r.shadows)).catch(() => undefined);
    api.researchPromotions().then((r) => setPromotions(r.promotions)).catch(() => undefined);
    api.researchKnowledge().then((r) => setKnowledge(r.entries)).catch(() => undefined);
    api.researchReports().then((r) => setReports(r.reports)).catch(() => undefined);
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, 15_000);
    return () => clearInterval(timer);
  }, [load]);

  const promote = async (candidate: ResearchCandidate) => {
    setError(null);
    setNotice(null);
    setBusy(candidate.candidate_id);
    try {
      // The API's own note is shown verbatim: it is the authority on what just
      // happened, and it is careful to say that nothing was activated.
      const result = await api.promoteCandidate(candidate.candidate_id);
      setNotice(result.note);
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  const labState = !status?.lab_enabled
    ? { dot: "unknown", text: "Disabled (RESEARCH_LAB_ENABLED=false)" }
    : status.running
      ? { dot: "healthy", text: "Running" }
      : { dot: "degraded", text: "Enabled — runtime not reporting" };

  return (
    <div className="space-y-6">
      <PageHeader
        title="Research"
        subtitle="Offline R&D on copied data — proposes only, with no path to execution."
      />

      {!reachable && (
        <div className="rounded-lg border border-red-500/30 bg-red-950/30 px-4 py-2 text-sm text-red-300">
          The research API is unreachable.
        </div>
      )}

      <div className="flex items-center gap-2">
        <StatusDot status={labState.dot} />
        <span className="text-sm text-gray-200">{labState.text}</span>
        {status?.lab_enabled === false && (
          <span className="text-xs text-hades-muted">
            · history below is still readable; only new studies are paused
          </span>
        )}
      </div>

      <div className="grid gap-4 sm:grid-cols-3 lg:grid-cols-5">
        <Stat label="Experiments" value={status?.experiments_total ?? 0} />
        <Stat label="Backtests" value={status?.backtests_total ?? 0} />
        <Stat label="Candidates" value={status?.candidates_total ?? 0} />
        <Stat label="Promotions" value={status?.promotions_total ?? 0} />
        <Stat label="Knowledge" value={status?.knowledge_total ?? 0} />
      </div>

      {error && (
        <div className="rounded-lg border border-red-500/30 bg-red-950/30 px-4 py-2 text-sm text-red-300">
          {error}
        </div>
      )}

      {notice && (
        <div className="rounded-lg border border-hades-accent/30 bg-hades-accent/5 px-4 py-2 text-sm text-gray-300">
          {notice}
        </div>
      )}

      <Tabs
        tabs={[
          {
            id: "experiments",
            label: "Experiments",
            element: (
              <Panel title="Recent experiments">
                {experiments.length === 0 ? (
                  <Empty what="experiments" />
                ) : (
                  <Table head={["Name", "Kind", "Status", "Conclusion", "When"]}>
                    {experiments.map((e) => (
                      <tr key={e.experiment_id}>
                        <td className="px-3 py-2 text-gray-200">{e.name}</td>
                        <td className="px-3 py-2 text-hades-muted">{e.kind}</td>
                        <td className="px-3 py-2">
                          <Badge tone={e.status === "completed" ? "success" : "neutral"}>
                            {e.status}
                          </Badge>
                        </td>
                        <td className="px-3 py-2 text-hades-muted">{e.conclusion ?? "—"}</td>
                        <td className="px-3 py-2 text-hades-muted">{when(e.at)}</td>
                      </tr>
                    ))}
                  </Table>
                )}
              </Panel>
            ),
          },
          {
            id: "backtests",
            label: "Backtests",
            element: (
              <Panel title="Recent backtests">
                {backtests.length === 0 ? (
                  <Empty what="backtests" />
                ) : (
                  <Table
                    head={["Strategy", "Return", "Sharpe", "Max DD", "PF", "Trades", "When"]}
                  >
                    {backtests.map((b) => (
                      <tr key={b.backtest_id}>
                        <td className="px-3 py-2 text-gray-200">{b.strategy}</td>
                        <td className="px-3 py-2">{num(b.total_return, 2, "%")}</td>
                        <td className="px-3 py-2">{num(b.sharpe)}</td>
                        <td className="px-3 py-2 text-red-300">{num(b.max_drawdown, 2, "%")}</td>
                        <td className="px-3 py-2">{num(b.profit_factor)}</td>
                        <td className="px-3 py-2 text-hades-muted">{b.trades ?? "—"}</td>
                        <td className="px-3 py-2 text-hades-muted">{when(b.at)}</td>
                      </tr>
                    ))}
                  </Table>
                )}
              </Panel>
            ),
          },
          {
            id: "shadows",
            label: "Shadow strategies",
            element: (
              <Panel title="Shadow strategies — virtual, zero capital">
                {shadows.length === 0 ? (
                  <Empty what="shadow strategies" />
                ) : (
                  <Table head={["Name", "Expectancy", "Trades", "Win rate"]}>
                    {shadows.map((s, i) => (
                      <tr key={String(s.name ?? i)}>
                        <td className="px-3 py-2 text-gray-200">{s.name ?? "—"}</td>
                        <td className="px-3 py-2">{num(s.expectancy)}</td>
                        <td className="px-3 py-2 text-hades-muted">{s.trades ?? "—"}</td>
                        <td className="px-3 py-2">{num(s.win_rate, 1, "%")}</td>
                      </tr>
                    ))}
                  </Table>
                )}
              </Panel>
            ),
          },
          {
            id: "candidates",
            label: "Candidates",
            element: (
              <div className="space-y-6">
                <Panel title="Candidate strategies">
                  <p className="mb-4 text-xs text-hades-muted">
                    Recording a promotion stores a governance decision. It deploys nothing,
                    activates no strategy and cannot enable live trading.
                  </p>
                  {candidates.length === 0 ? (
                    <Empty what="candidates" />
                  ) : (
                    <Table head={["Name", "Archetype", "Version", "Stage", ""]}>
                      {candidates.map((c) => (
                        <tr key={c.candidate_id}>
                          <td className="px-3 py-2 text-gray-200">{c.name}</td>
                          <td className="px-3 py-2 text-hades-muted">{c.archetype ?? "—"}</td>
                          <td className="px-3 py-2 text-hades-muted">{c.version ?? "—"}</td>
                          <td className="px-3 py-2">
                            <Badge tone="neutral">{c.stage}</Badge>
                          </td>
                          <td className="px-3 py-2 text-right">
                            <button
                              type="button"
                              disabled={busy === c.candidate_id}
                              onClick={() => promote(c)}
                              className="rounded-md border border-white/10 px-3 py-1 text-xs text-gray-300 transition-colors hover:border-hades-accent/50 hover:text-hades-accent disabled:opacity-40"
                            >
                              {busy === c.candidate_id ? "Recording…" : "Record promotion"}
                            </button>
                          </td>
                        </tr>
                      ))}
                    </Table>
                  )}
                </Panel>

                <Panel title="Promotion decisions">
                  {promotions.length === 0 ? (
                    <Empty what="promotion decisions" />
                  ) : (
                    <Table head={["Candidate", "Kind", "Outcome", "Manual", "When"]}>
                      {promotions.map((p, i) => (
                        <tr key={`${p.candidate_id}-${i}`}>
                          <td className="px-3 py-2 text-gray-200">
                            {p.candidate_name ?? p.candidate_id}
                          </td>
                          <td className="px-3 py-2 text-hades-muted">{p.kind ?? "—"}</td>
                          <td className="px-3 py-2">
                            <Badge tone={p.outcome === "approved" ? "success" : "neutral"}>
                              {p.outcome}
                            </Badge>
                          </td>
                          <td className="px-3 py-2 text-hades-muted">
                            {p.manual_approved ? "yes" : "no"}
                          </td>
                          <td className="px-3 py-2 text-hades-muted">{when(p.at)}</td>
                        </tr>
                      ))}
                    </Table>
                  )}
                </Panel>
              </div>
            ),
          },
          {
            id: "knowledge",
            label: "Knowledge",
            element: (
              <div className="space-y-6">
                <Panel title="Knowledge base — append-only">
                  {knowledge.length === 0 ? (
                    <Empty what="knowledge entries" />
                  ) : (
                    <ul className="space-y-3">
                      {knowledge.map((k) => (
                        <li
                          key={k.entry_id}
                          className="rounded-lg border border-white/5 bg-black/20 p-4"
                        >
                          <div className="flex items-center justify-between gap-3">
                            <span className="text-sm font-medium text-gray-100">{k.title}</span>
                            <Badge tone="neutral">{k.category}</Badge>
                          </div>
                          {k.body && (
                            <p className="mt-2 text-sm text-hades-muted">{k.body}</p>
                          )}
                          <p className="mt-2 text-xs text-hades-muted/60">{when(k.at)}</p>
                        </li>
                      ))}
                    </ul>
                  )}
                </Panel>

                <Panel title="Reports">
                  {reports.length === 0 ? (
                    <Empty what="reports" />
                  ) : (
                    <Table head={["Title", "Period", "Summary", "When"]}>
                      {reports.map((r) => (
                        <tr key={r.report_id}>
                          <td className="px-3 py-2 text-gray-200">{r.title}</td>
                          <td className="px-3 py-2 text-hades-muted">{r.period ?? "—"}</td>
                          <td className="px-3 py-2 text-hades-muted">{r.summary ?? "—"}</td>
                          <td className="px-3 py-2 text-hades-muted">{when(r.at)}</td>
                        </tr>
                      ))}
                    </Table>
                  )}
                </Panel>
              </div>
            ),
          },
        ]}
      />
    </div>
  );
}
