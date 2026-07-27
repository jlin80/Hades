// Typed API client. All transport concerns (REST + WebSocket URLs) live here so
// screens depend only on typed methods, never on fetch details.

// VITE_API_BASE_URL is baked in at build time, so a fixed value (e.g.
// "http://localhost:8000") only works when the dashboard is opened from the same
// host the API runs on. Absent an explicit override, resolve the API against
// whatever host served this page (LAN IP, tailscale name, etc.) at runtime instead,
// so one build works from any machine on the network.
const BASE_URL =
  import.meta.env.VITE_API_BASE_URL ?? `${window.location.protocol}//${window.location.hostname}:8000`;

export interface Meta {
  name: string;
  version: string;
  environment: string;
  trading_mode: string;
  is_live: boolean;
  event_bus_transport: string;
  contexts: string[];
}

export interface Health {
  status: string;
  instance_id: string;
  trading_mode: string;
  is_live: boolean;
  components: { name: string; status: string; detail: string }[];
}

export interface Status {
  name: string;
  version: string;
  role: string;
  environment: string;
  instance_id: string;
  trading_mode: string;
  is_live: boolean;
  live_gate_enabled: boolean;
  event_bus_transport: string;
  uptime_seconds: number;
}

export interface ModeStatus {
  mode: string;
  is_live: boolean;
  live_gate_enabled: boolean;
}

export interface ReadinessCheck {
  name: string;
  ok: boolean;
  detail: string;
  required: boolean;
}

export interface ReadinessReport {
  ready: boolean;
  checks: ReadinessCheck[];
}

export interface LogRecord {
  event?: string;
  level?: string;
  logger?: string;
  timestamp?: string;
  [key: string]: unknown;
}

export interface RpcEndpoint {
  name: string;
  status: string;
  priority: number;
  latency_ms: number;
  health_score: number;
  total_calls: number;
  total_failures: number;
  total_rate_limited: number;
  enabled: boolean;
}

export interface ScannerLive {
  rpc_active?: string | null;
  rpc_endpoints?: RpcEndpoint[];
  queue_depth?: number;
  active_workers?: number;
  worker_count?: number;
  sources?: Record<string, boolean>;
  sources_available?: number;
  sources_total?: number;
  features_total?: number;
  features_per_second?: number;
  updated_at?: number;
}

export interface ScannerStatus {
  enabled: boolean;
  running: boolean;
  configured_sources: string[];
  tokens_total: number;
  tokens_active_1h: number;
  anomalies_total: number;
  live: ScannerLive;
}


// --- Portfolio ---------------------------------------------------------------

export interface PortfolioLive {
  starting_balance_usd?: number;
  equity_usd?: number;
  cash_usd?: number;
  invested_usd?: number;
  realized_pnl_usd?: number;
  unrealized_pnl_usd?: number;
  roi_pct?: number;
  drawdown_pct?: number;
  exposure_pct?: number;
  open_positions?: number;
  reserve_pct?: number;
  available_usd?: number;
  metrics?: Record<string, number>;
  updated_at?: number;
}

export interface PortfolioStatus {
  running: boolean;
  live: PortfolioLive;
}

export interface PnlRow {
  at: string | null;
  kind: string;
  mode: string;
  amount_usd: number;
}

export interface EquityPoint {
  t: string | null;
  equity_usd: number;
}

export interface ExecutionMetrics {
  mode?: string;
  is_live?: boolean;
  live_executor_available?: boolean;
  counts?: Record<string, number>;
  transaction_stats?: Record<string, number>;
}

export interface AnomalyRow {
  subject: string;
  kind: string;
  field: string;
  detail: string;
  at: string | null;
}

// --- Wallet Intelligence -----------------------------------------------------

export interface IntelligenceStatus {
  enabled: boolean;
  running: boolean;
  wallets_total: number;
  smart_money_total: number;
  clusters_total: number;
  live: {
    wallets_registered_session?: number;
    clusters_session?: number;
    live_lookups?: boolean;
    rpc_active?: string | null;
    updated_at?: number;
  };
}

export interface WalletReputation {
  trust: number;
  risk: number;
  consistency: number;
  experience: number;
  profitability: number;
}

export interface WalletExplanation {
  summary: string;
  positives: string[];
  negatives: string[];
}

export interface FundingLink {
  source: string;
  kind: string;
  note?: string;
}

export interface WalletProfile {
  address: string;
  first_seen_at: string | null;
  last_active_at: string | null;
  observations: number;
  tokens_traded: number;
  launches: number;
  rugs_participated: number;
  successes: number;
  dexes: string[];
  roles: string[];
  avg_pct_supply: number;
  reputation: WalletReputation;
  behavior: string;
  behavior_confidence: number;
  money_class: string;
  smart_money_score: number;
  influence: number;
  funding: FundingLink[];
  cluster_id: string | null;
  wallet_score: number;
  band: string;
  explanation: WalletExplanation | null;
  version: number;
}

export interface WalletTimelineEntry {
  at: string | null;
  kind: string;
  mint: string | null;
  detail: string;
}

export interface WalletRelationship {
  source: string;
  target: string;
  kind: string;
  weight: number;
  observations: number;
}

export interface IntelCluster {
  cluster_id: string;
  funder: string | null;
  member_count: number;
  reason: string;
  confidence: number;
  pct_supply: number;
  detected_at: string | null;
}

// --- AI Committee ------------------------------------------------------------

export interface CommitteeStatus {
  enabled: boolean;
  running: boolean;
  models_total: number;
  promoted_total: number;
  predictions_total: number;
  live: {
    members?: number;
    shadow_enabled?: boolean;
    auto_train?: boolean;
    predictions_session?: number;
    last_prediction?: {
      mint?: string;
      prob_roi_positive?: number;
      prob_hit_tp?: number;
      prob_hit_sl?: number;
      confidence?: number;
      regime?: string;
      headline?: string;
      at?: string;
    };
    updated_at?: number;
  };
}

export interface CommitteeModel {
  model_id: string;
  name: string;
  version: string;
  kind: string;
  status: string;
  dataset_id: string | null;
  feature_names: string[];
  metrics: Record<string, number>;
  trained_on_samples: number;
  notes: string;
  created_at: string | null;
  promoted_at: string | null;
}

export interface CommitteePredictionSummary {
  mint: string;
  symbol: string | null;
  at: string | null;
  prob_roi_positive: number;
  prob_hit_tp: number;
  prob_hit_sl: number;
  confidence: number;
  regime: string;
  feature_coverage: number;
  shadow: boolean;
  headline: string;
}

export interface CommitteeReason {
  feature: string;
  text: string;
  contribution: number;
  direction: string;
}

export interface CommitteeOpinion {
  member: string;
  probability: number;
  confidence: number;
  summary: string;
  reasons: CommitteeReason[];
  positives: string[];
  negatives: string[];
  features_used: number;
  model_version: string;
  abstained: boolean;
}

export interface CommitteePredictionDetail {
  token: { mint: { address: string }; symbol?: string | null };
  at: string;
  meta: {
    prob_roi_positive: number;
    prob_hit_tp: number;
    prob_hit_sl: number;
    confidence: number;
    member_probabilities: Record<string, number>;
    model_version: string;
  };
  opinions: CommitteeOpinion[];
  confidence: {
    dataset_quality: number;
    sample_support: number;
    coverage: number;
    agreement: number;
    regime_stability: number;
    volatility_penalty: number;
    final: number;
  };
  regime: { regime: string; confidence: number; scores: Record<string, number>; detail: string };
  explanation: { headline: string; drivers: string[]; risks: string[]; caveats: string[] } | null;
  model_versions: Record<string, string>;
  feature_coverage: number;
}

export interface FeatureImportanceView {
  model_id: string;
  importance: {
    computed_at: string | null;
    importances: Record<string, number>;
    useless: string[];
    redundant: string[];
    stale: string[];
  } | null;
}

export interface DriftAlert {
  model_id: string;
  kind: string;
  severity: number;
  triggered: boolean;
  detail: string;
  feature_drifts: Record<string, number>;
  at: string | null;
}

// --- Research Lab ------------------------------------------------------------
// The lab only ever *proposes*. Nothing on these screens deploys anything, and
// a promotion is a recorded governance decision, never an activation.

export interface ResearchStatus {
  lab_enabled: boolean;
  running: boolean;
  experiments_total: number;
  backtests_total: number;
  candidates_total: number;
  promotions_total: number;
  knowledge_total: number;
  live: {
    shadow_strategies?: ShadowStrategy[];
    [key: string]: unknown;
  };
}

export interface ShadowStrategy {
  name?: string;
  expectancy?: number;
  trades?: number;
  win_rate?: number;
  [key: string]: unknown;
}

export interface ResearchExperiment {
  experiment_id: string;
  name: string;
  kind: string;
  status: string;
  hypothesis: string | null;
  conclusion: string | null;
  metrics: Record<string, unknown> | null;
  at: string | null;
}

export interface ResearchBacktest {
  backtest_id: string;
  strategy: string;
  total_return: number | null;
  sharpe: number | null;
  max_drawdown: number | null;
  profit_factor: number | null;
  trades: number | null;
  at: string | null;
}

export interface ResearchCandidate {
  candidate_id: string;
  name: string;
  archetype: string | null;
  version: string | null;
  stage: string;
  payload: Record<string, unknown> | null;
}

export interface ResearchPromotion {
  candidate_id: string;
  candidate_name: string | null;
  kind: string | null;
  outcome: string;
  manual_approved: boolean;
  rationale: string | null;
  at: string | null;
}

export interface ResearchKnowledgeEntry {
  entry_id: string;
  category: string;
  title: string;
  body: string | null;
  data: Record<string, unknown> | null;
  at: string | null;
}

export interface ResearchHypothesis {
  hypothesis_id: string;
  statement: string;
  status: string;
  rationale: string | null;
  evidence: Record<string, unknown> | null;
  at: string | null;
}

export interface ResearchReport {
  report_id: string;
  period: string | null;
  title: string;
  summary: string | null;
  payload: Record<string, unknown> | null;
  at: string | null;
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`);
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return (await res.json()) as T;
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      const data = await res.json();
      detail = data.message ?? data.detail ?? detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

export function wsUrl(path: string): string {
  const base = BASE_URL.replace(/^http/, "ws");
  return `${base}${path}`;
}

export const api = {
  meta: () => get<Meta>("/api/v1/meta"),
  health: () => get<Health>("/health"),
  status: () => get<Status>("/api/v1/status"),
  info: () => get<Record<string, unknown>>("/api/v1/info"),
  config: () => get<Record<string, unknown>>("/api/v1/config"),
  scannerStatus: () => get<ScannerStatus>("/api/v1/scanner/status"),
  scannerAnomalies: () =>
    get<{ anomalies: AnomalyRow[]; by_kind: Record<string, number> }>(
      "/api/v1/scanner/anomalies?limit=50",
    ),
  portfolio: () => get<PortfolioStatus>("/api/v1/portfolio"),
  portfolioCapital: () => get<PortfolioLive>("/api/v1/portfolio/capital"),
  portfolioPnl: () => get<{ pnl: PnlRow[] }>("/api/v1/portfolio/pnl?limit=50"),
  portfolioEquityCurve: () =>
    get<{ points: EquityPoint[] }>("/api/v1/portfolio/equity-curve?limit=200"),
  executionMetrics: () => get<ExecutionMetrics>("/api/v1/execution/metrics"),
  intelligenceStatus: () => get<IntelligenceStatus>("/api/v1/intelligence/status"),
  walletProfile: (address: string) =>
    get<WalletProfile>(`/api/v1/intelligence/wallet/${address}`),
  walletTimeline: (address: string) =>
    get<{ address: string; timeline: WalletTimelineEntry[] }>(
      `/api/v1/intelligence/wallet/${address}/timeline`,
    ),
  walletRelationships: (address: string) =>
    get<{ address: string; relationships: WalletRelationship[] }>(
      `/api/v1/intelligence/wallet/${address}/relationships`,
    ),
  intelligenceClusters: () =>
    get<{ clusters: IntelCluster[] }>("/api/v1/intelligence/clusters"),
  smartMoney: () =>
    get<{ wallets: WalletProfile[] }>("/api/v1/intelligence/smart-money"),
  tradingMode: () => get<ModeStatus>("/api/v1/trading/mode"),
  verifyLive: () => post<ReadinessReport>("/api/v1/trading/mode/verify"),
  changeMode: (mode: string, confirm: boolean) =>
    post<ModeStatus>("/api/v1/trading/mode", { mode, confirm }),
  // AI Committee.
  committeeStatus: () => get<CommitteeStatus>("/api/v1/committee/status"),
  committeeModels: () => get<{ models: CommitteeModel[] }>("/api/v1/committee/models"),
  committeeVersions: (name: string) =>
    get<{ name: string; versions: CommitteeModel[] }>(
      `/api/v1/committee/models/${name}/versions`,
    ),
  committeePredictions: (limit = 50) =>
    get<{ predictions: CommitteePredictionSummary[] }>(
      `/api/v1/committee/predictions?limit=${limit}`,
    ),
  committeePrediction: (mint: string) =>
    get<CommitteePredictionDetail>(`/api/v1/committee/predictions/${mint}`),
  committeeFeatureImportance: (modelId: string) =>
    get<FeatureImportanceView>(`/api/v1/committee/models/${modelId}/feature-importance`),
  committeeDrift: () => get<{ drift: DriftAlert[] }>("/api/v1/committee/drift"),
  promoteModel: (modelId: string) =>
    post<{ promoted: boolean; model_id: string; name: string; version: string; status: string }>(
      `/api/v1/committee/models/${modelId}/promote`,
    ),
  // Research Lab — read-only except the human-gated promotion record.
  researchStatus: () => get<ResearchStatus>("/api/v1/research/status"),
  researchExperiments: (limit = 50) =>
    get<{ experiments: ResearchExperiment[] }>(`/api/v1/research/experiments?limit=${limit}`),
  researchBacktests: (limit = 50) =>
    get<{ backtests: ResearchBacktest[] }>(`/api/v1/research/backtests?limit=${limit}`),
  researchCandidates: () =>
    get<{ candidates: ResearchCandidate[] }>("/api/v1/research/candidates"),
  researchShadows: () =>
    get<{ shadows: ShadowStrategy[]; source: string }>("/api/v1/research/shadows"),
  researchRankings: () =>
    get<{ ranking: string[]; entries: ShadowStrategy[] }>("/api/v1/research/rankings"),
  researchPromotions: (limit = 50) =>
    get<{ promotions: ResearchPromotion[] }>(`/api/v1/research/promotions?limit=${limit}`),
  researchKnowledge: (limit = 100) =>
    get<{ entries: ResearchKnowledgeEntry[] }>(`/api/v1/research/knowledge?limit=${limit}`),
  researchHypotheses: (limit = 100) =>
    get<{ hypotheses: ResearchHypothesis[] }>(`/api/v1/research/hypotheses?limit=${limit}`),
  researchReports: (limit = 50) =>
    get<{ reports: ResearchReport[] }>(`/api/v1/research/reports?limit=${limit}`),
  // Fail-closed: the API rejects this without an explicit approve=true. It
  // records a decision and returns a note saying nothing was activated.
  promoteCandidate: (candidateId: string) =>
    post<{ candidate_id: string; name: string; recorded: boolean; note: string }>(
      `/api/v1/research/candidates/${candidateId}/promote?approve=true`,
    ),
};
