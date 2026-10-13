import { webClient } from './client'

export type Verdict = 'none' | 'edge' | 'marginal' | 'no-edge' | 'inconclusive'

/** Headline metrics of a run. Optional keys are omitted by the backend when unknown. */
export interface RunMetrics {
  trades: number
  gross_bps: number
  net_bps: number
  win_rate?: number
  sharpe?: number
  t_stat?: number
  hurdle_t?: number
  profit_factor?: number
  expectancy_r?: number
  payoff?: number
  max_dd_r?: number
  days?: number
  breakeven_cost_bps?: number
  peak_concurrent?: number
  median_concurrent?: number
  [key: string]: number | undefined
}

/** How far back a run tested and how stale its data snapshot is. */
export interface CoverageFields {
  years: number | null
  data_age_days: number | null
}

/** Result if only `max_positions` trades could be open at once. */
export interface PositionCap {
  max_positions: number
  trades: number
  taken_pct?: number
  gross_bps: number
  net_bps: number
  win_rate?: number
  t_stat?: number
  sharpe?: number
}

/** Same names, same holding period, random entry days, costs deducted. */
export interface Baseline {
  method?: string
  draws_per_trade?: number
  trades?: number
  skipped?: number
  gross_bps: number
  net_bps: number
  trade_net_bps?: number
  excess_net_bps: number
  excess_t_stat?: number
}

export interface SplitMetrics {
  trades: number
  gross_bps: number
  net_bps: number
  win_rate?: number
  sharpe?: number
  t_stat?: number
}

export interface LatestRun {
  run_id: string
  date: string
  code_version: string
  parent_run_id: string | null
  change_note: string
  metrics: RunMetrics
  n_trials: number
  promoted: boolean
  oos_net_bps: number | null
  net_bps_cap10?: number | null
  baseline_excess_bps?: number | null
  baseline_excess_t?: number | null
  /**
   * Coverage fields are optional on purpose: an app server started before the backend
   * gained them still answers with the older shape, and the page must degrade to n/a.
   */
  period?: { start: string; end: string }
  data?: { source: string; as_of: string; universe: string; bar_size: string }
  years?: number | null
  data_age_days?: number | null
  sparkline: number[]
  /** Calculated verdict of this latest run, when the server sends it. */
  run_verdict?: RunVerdictValue | null
}

export interface IdeaCard {
  id: string
  title: string
  status: string
  verdict: Verdict
  horizon: string
  summary: string
  result?: string
  revisit_when?: string
  last_reviewed?: string
  source?: { type?: string; author?: string; ref?: string }
  /** Why a rejected idea was rejected; absent on older ideas. */
  rejection_basis?: string
  /** Why a blocked idea cannot be run yet. Blocked is not evidence: the idea is untested. */
  block_reason?: string
  /** Workflow stage, set by the researcher and gated by evidence (not the legacy `status`). */
  stage?: Stage
  /** The researcher's own disposition. Separate from any run's calculated verdict. */
  research_state?: ResearchState
  /** What kind of strategy this is. Idea-level, mutable, never part of a run or a version. */
  strategy_category?: string
  strategy_tags?: string[]
  classification?: Classification
  run_count: number
  latest_run: LatestRun | null
}

export interface ResultsSummary {
  total_ideas: number
  total_runs: number
  ideas_with_runs: number
  by_status: Record<string, number>
  by_verdict: Record<string, number>
}

export type SeriesPoint = [string, number]

export interface MonthlyRow {
  month: string
  n: number
  total_r: number
  net_r: number
}

export interface CostSensitivityPoint {
  round_trip_bps: number
  net_bps: number
}

export type GateStatus = 'pass' | 'fail' | 'report' | 'not_evaluated' | 'not_applicable'

export interface GateRow {
  gate: string
  title: string
  status: GateStatus
  hard: boolean
  value: number | string | null
  threshold: string
  evidence: string
}

export interface GateDimension {
  name: string
  rows: GateRow[]
}

/** Strategy evaluation profile. There is deliberately no composite score. */
export interface EvaluationBlock {
  version: number
  criteria?: string
  horizon?: string
  dimensions: GateDimension[]
  summary: {
    counts: Partial<Record<GateStatus, number>>
    hard_failed: string[]
  }
}

export interface RegimeRow {
  regime: string
  trades: number
  net_r: number
  total_r: number
  win_rate: number
  max_dd_r: number
  low_sample: boolean
}

/** Performance by market regime; each trade is placed by its entry day's regime. */
export interface RegimesBlock {
  version: number
  min_trades?: number
  unclassified_trades?: number
  trend: RegimeRow[]
  vol: RegimeRow[]
  source?: Record<string, unknown>
}

export interface Lineage {
  runs: number
  latest_run_id: string
  trials_sum: number
  trend: 'improving' | 'stagnant' | 'regressing' | 'insufficient'
  /** Whether the latest run's t-statistic clears `edge_bar`, independent of the trend. */
  edge_status?: 'clears_bar' | 'below_bar' | 'unknown'
  edge_bar?: number
  delta_vs_parent: {
    net_bps: number
    t_stat?: number
    trades: number
    parent_run_id: string
  } | null
}

/** The canonical strategy definition (spec.yaml); every field is optional for display. */
export interface StrategySpec {
  name?: string
  market?: string
  timeframe?: string
  trading_style?: string
  direction?: string
  entry?: string | string[]
  exit?: string | string[]
  stop_loss?: string | string[]
  take_profit?: string | string[]
  position_sizing?: string | string[]
  max_holding_period?: string | string[]
  filters?: string | string[]
  session_filters?: string | string[]
  indicators?: unknown
  parameters?: Record<string, unknown>
  cost_assumptions?: string | string[]
  constraints?: string | string[]
  rationale?: string | string[]
  // Current frozen-spec schema (spec v2). The legacy names above are still accepted.
  style?: string
  stop?: unknown
  sizing?: unknown
  holding_period?: unknown
  re_entry?: unknown
  timing?: unknown
  session_window?: unknown
  [key: string]: unknown
}

// ---- Run decision, scope and reproducibility (build order 5) -------------------------------
// Calculated by signal_engine and read from the run folder. The UI only presents them.

export type Stage = 'inbox' | 'backtest' | 'paper' | 'live' | 'retired'
export type ResearchState =
  | 'active'
  | 'promising'
  | 'needs_more_evidence'
  | 'weak_evidence'
  | 'evidence_against_edge'
  | 'shelved'
  | 'rejected'
  | 'blocked'

/** Calculated per run, never typed, never changes once the run is written. */
export type RunVerdictValue = 'EDGE' | 'MARGINAL' | 'NO_EDGE' | 'INCONCLUSIVE'
export type PromotionDecision = 'READY_FOR_NEXT_STAGE' | 'NOT_READY' | 'BLOCKED'
export type PromotionGateStatus = 'PASS' | 'FAIL' | 'NOT_RUN' | 'NOT_APPLICABLE' | 'REPORT'

export interface VerdictReason {
  text: string
  gate: string | null
  status: string | null
  /** For example `evaluation:G2.1`: the gate row that backs this reason. */
  evidence_ref: string | null
}

export interface DerivedVerdict {
  run_verdict: RunVerdictValue
  primary_failed_gate: string | null
  reasons: VerdictReason[]
  /** For example `engine readiness: ...` when EDGE was held at MARGINAL. */
  caps_applied: string[]
  /** Limitations that qualify the verdict without capping it, e.g. a survivor-biased universe. */
  qualifications?: string[]
}

export interface PromotionStageResult {
  decision: PromotionDecision
  gates: { gate: string; status: PromotionGateStatus }[]
  reasons: string[]
}

export interface Promotion {
  stages: {
    backtest: PromotionStageResult
    paper: PromotionStageResult
    live: PromotionStageResult
  }
  next_stage: string | null
}

export interface NextAction {
  action: string
  reason: string
  gate: string | null
}

export interface Classification {
  strategy_category: string
  strategy_tags: string[]
  trading_style: string | null
  direction: string | null
  timeframe: string | null
  market: string | null
}

export type AssumptionSource =
  | 'user'
  | 'derived'
  | 'platform_default'
  | 'market_data'
  | 'broker_config'
  | 'inferred'

export interface AssumptionEntry {
  id: string
  field: string
  section: 'intent' | 'execution' | 'profile'
  value: unknown
  source: AssumptionSource
  confidence: 'high' | 'medium' | 'low'
  materiality: 'high' | 'medium' | 'low'
  reason: string
  user_confirmed: boolean
}

/** One reproduction attempt, stored outside the run folder. */
export interface ReproductionAttempt {
  attempted_ist: string
  reproducible: boolean
  reasons: string[]
}

export interface RunScope {
  market: string | null
  universe: string | null
  symbol_count: number | null
  style: string | null
  direction: string | null
  timeframe: string | null
  product: string | null
  period: { start: string; end: string } | null
  /** Strategy session, exchange session and broker cutoff are separate things. */
  strategy_window: string | { entry?: string; no_trade?: string[] } | null
  exchange_session: string | null
  broker_cutoff: string | null
  force_flat: string | null
  capital_per_trade: number | null
  risk_per_trade_pct: number | null
  portfolio_mode: string | null
  portfolio_scope: string | null
  slippage_bps_per_side: number | null
  cost_bps_round_trip: number | null
  profile: { name: string; sha256: string } | null
  dataset: {
    id: string
    source: string
    interval: string
    data_as_of: string
    fingerprint: string
  } | null
}

export interface RunReproducibility {
  reproducible: boolean
  code_dirty: boolean
  dirty_paths: string[]
  code_commit: string | null
  engine_version: string | null
  /** Each reproducibility item and whether it was captured. */
  checklist: Record<string, boolean>
  last_attempt: ReproductionAttempt | null
}

export interface RunFingerprints {
  rules: string
  params: string
  strategy: string
  execution: string
  cost: string
  universe: string
  profile: string
  dataset: string
}

/**
 * Everything a run folder says about one run. `captured` is false for a schema 2 record that has
 * no run folder: every other field is then null or empty and the UI shows "not captured".
 */
export interface RunDetail {
  run_id: string
  schema: number
  captured: boolean
  strategy_version: string | null
  parent_version: string | null
  branch_label: string | null
  change_classes: string[]
  verdict: DerivedVerdict | null
  promotion: Promotion | null
  next_action: NextAction | null
  /** The frozen intent this run was made from, never the idea's current spec.yaml. */
  spec: Record<string, unknown> | null
  fingerprints: RunFingerprints | null
  scope: RunScope | null
  reproducibility: RunReproducibility | null
  assumptions: { sealed_ist: string; entries: AssumptionEntry[] } | null
  missing_capabilities: string[]
  manifest_sha256: string | null
  created_ist: string | null
  /** What actually ran at record time. Null for a run recorded before validity checks existed. */
  validity: ValidityBlock | null
  data_quality: DataQualityReport | null
  trials: TrialsBlock | null
  ledgers: LedgerSummary | null
  cost_components: CostComponentSummary | null
  /** True when evidence/trades.parquet exists, so the trade drill-down can open. */
  has_trades: boolean
  /** True when the lineage ledgers exist, so a trade can be followed to its signal and fills. */
  has_ledgers: boolean
}

// ---- Evidence, data quality, cost decomposition and trade drill-down (build order 8) ---------
// Read from the run folder's report.json, data_quality.json and Parquet evidence. Presentation only.

export interface DataQualityItem {
  check: string
  severity: 'info' | 'warning'
  detail: string
  count: number
  /** Up to ten symbol names affected. */
  symbols: string[]
}

export interface DataQualityReport {
  schema: number
  status: 'PASS' | 'WARNING' | 'NOT_RUN'
  /** A WARNING that affects validity makes the run INCONCLUSIVE. */
  affects_validity: boolean
  thresholds: Record<string, number>
  items: DataQualityItem[]
  summary: Record<string, unknown>
  dropped: Record<string, unknown>
  /** Plain statements of what the data layer does and does not handle. */
  policy: { corporate_actions: string; closing_auction: string; timestamps: string }
}

export interface ValidityBlock {
  ok: boolean
  failures: { check: string; detail: string }[]
  /** The checks that actually ran. A check not listed here did not run and is not a pass. */
  checks_run: string[]
}

export interface TrialsBlock {
  material: number
  external: number
  total: number
  /** Where the number came from: 'trials.yaml' (the ledger). */
  source: string
}

export interface LedgerSummary {
  signals: number
  accepted: number
  excluded_by_reason: Record<string, number>
  orders: number
  filled: number
  dropped: number
  fills: number
  trades: number
  reconcile_problems: number
  problems: string[]
}

export interface CostComponent {
  /** For example 'brokerage', 'stt', 'exchange', 'gst', 'sebi', 'stamp', 'slippage'. */
  key: string
  label: string
  mean_bps: number
}

/** Modelled per-trade costs (basis points of entry notional), averaged over the run's trades. */
export interface CostComponentSummary {
  trades: number
  components: CostComponent[]
  /** The sum of the components. The headline net figures use the engine's flat cost instead. */
  modeled_cost_bps_mean: number
  engine_cost_bps_mean: number | null
}

export type TradeSubset = 'all' | 'in_sample' | 'oos' | 'losers' | 'gap_exits'

export interface TradeRow {
  trade_id: string
  symbol: string
  side: string
  entry_time: string
  exit_time: string | null
  entry_price: number | null
  exit_price: number | null
  exit_reason: string | null
  gross_r: number | null
  net_r: number | null
  gross_bps: number | null
  net_bps: number | null
  cost_bps: number | null
  signal_id?: string | null
  order_id?: string | null
  entry_fill_id?: string | null
  exit_fill_id?: string | null
  position_id?: string | null
  [cost_component: string]: unknown
}

export interface TradesPage {
  total: number
  limit: number
  offset: number
  subset: TradeSubset
  /** What the subset means, for example that purge and embargo exclusions are not applied. */
  subset_note?: string | null
  /** Set when the page is empty for a stated reason (for example no out-of-sample split). */
  message?: string | null
  rows: TradeRow[]
}

/** The chain behind one trade, from its ids. Each part is null when the run has no ledgers. */
export interface TradeLineage {
  trade_id: string
  signal: Record<string, unknown> | null
  order: Record<string, unknown> | null
  fills: Record<string, unknown>[]
  position_id: string | null
  /** Parts that could not be resolved because the run's files lack the columns or ledgers. */
  missing?: string[]
}

export interface TradesQuery {
  subset?: TradeSubset
  symbol?: string
  reason?: string
  sort?: string
  desc?: boolean
  limit?: number
  offset?: number
}

export interface BacktestTradesResponse extends Envelope {
  page?: TradesPage
}

export interface BacktestLineageResponse extends Envelope {
  lineage?: TradeLineage
}

export interface BacktestRunDetailResponse extends Envelope {
  run: RunDetail
}

export interface RunRecord {
  run_id: string
  date: string
  code_version: string
  data: { source: string; as_of: string; universe: string; bar_size: string }
  period: { start: string; end: string }
  costs: { model: string; round_trip_bps: number }
  metrics: RunMetrics
  params?: Record<string, unknown>
  parent_run_id?: string | null
  change_note?: string
  n_trials?: number
  promoted?: boolean
  cost_sensitivity?: CostSensitivityPoint[]
  position_caps?: PositionCap[]
  baseline?: Baseline
  years?: number | null
  data_age_days?: number | null
  split?: { type: string; oos_start: string }
  in_sample?: SplitMetrics
  out_of_sample?: SplitMetrics
  series?: {
    equity_r?: SeriesPoint[]
    drawdown_r?: SeriesPoint[]
    monthly?: MonthlyRow[]
  }
  validation?: string[]
  notes?: string
  evaluation?: EvaluationBlock
  regimes?: RegimesBlock
  /** True when a per-trade file is stored beside the record. */
  trades_export?: boolean
  /** Schema 3 flat-record fields. Absent on a schema 2 run. */
  schema?: number
  strategy_version?: string
  strategy_fingerprint?: string
  change_classes?: string[]
  reproducible?: boolean
  code_dirty?: boolean
  promotion_eligible?: boolean
  missing_capabilities?: string[]
  run_verdict?: RunVerdictValue | null
  promotion_decision?: PromotionDecision | null
  next_action?: NextAction | null
}

export interface MetricDelta {
  a: number | null
  b: number | null
  delta: number | null
}

export interface RunRef {
  run_id: string
  date: string
  code_version: string
  change_note?: string
}

export interface ParamDiff {
  changed: Record<string, { a: unknown; b: unknown }>
  added: Record<string, unknown>
  removed: Record<string, unknown>
}

interface Envelope {
  status: 'success' | 'error'
  message?: string
}

export interface BacktestListResponse extends Envelope {
  available?: boolean
  ideas?: IdeaCard[]
  summary?: ResultsSummary
}

// ---- Strategy versions (build order 7) -----------------------------------------------------
// Read from the idea's versions.yaml (append-only) and its frontmatter pointers.

export interface StrategyVersion {
  /** v1, v2, ... or a branch label such as v3b. */
  version: string
  strategy_fingerprint: string
  /** The version this one descends from. Null for the first version. */
  parent: string | null
  created_ist: string
  note: string
}

/** Current, latest and promoted are three different things and are marked separately. */
export interface VersionPointers {
  current_version: string | null
  promoted_version: string | null
  promoted_run_id: string | null
}

export interface BacktestIdeaResponse extends Envelope {
  idea?: Omit<IdeaCard, 'run_count' | 'latest_run'>
  runs?: RunRecord[]
  lineage?: Lineage | null
  /** Null when the idea has no spec.yaml on file. */
  spec?: StrategySpec | null
  /** The idea.md body: research notes and the decision log, apart from the definition. */
  research_notes?: string
  /** Every strategy version in creation order. Null when versions.yaml is absent or unreadable. */
  versions?: StrategyVersion[] | null
  /** A plain sentence when versions.yaml exists but could not be read; null when it is fine or absent. */
  versions_error?: string | null
  version_pointers?: VersionPointers
  /** Every parameter-tuning record of the idea, oldest first. Empty when no tuning ran. */
  tuning?: TuningSummary[]
}

/** One grid combination of a tuning record and its neighbourhood (itself and one step around). */
export interface TunedCombination {
  values: Record<string, unknown>
  trades: number | null
  net_r: number | null
  region_worst_net_r: number | null
  region_median_net_r: number | null
  region_size: number | null
  /** Grid points within one step in every parameter; region_size below it means gaps. */
  region_possible: number | null
}

/** Headline of a tuning record (backtest/tuning.py). Read only; the UI never re-ranks. */
export interface TuningSummary {
  tuning_id: string
  /** Set when the record could not be read; every other field is then absent. */
  error?: string
  created_ist?: string | null
  objective?: string | null
  grid?: Record<string, unknown[]> | null
  min_trades?: number | null
  in_sample_end?: string | null
  held_out_from?: string | null
  combinations?: number | null
  usable?: number
  selected?: TunedCombination | null
  best_single?: TunedCombination | null
  selected_is_not_the_single_best?: boolean | null
  /** Diagnostic only: measures single-best selection, no gate reads it. */
  pbo?: { value: number | null; role: 'diagnostic' }
  /** True only when every setting was ranked with the live portfolio entry limits applied. */
  constraints?: { fully_constrained: boolean; basis: string | null; unconstrained: number }
}

// ---- Run comparison (build order 6) ---------------------------------------------------------
// Computed by signal_engine (backtest/compare.py) from two run folders. The UI only presents it.
// Deltas are never labelled better or worse: that is the researcher's call.

/** One compared field. `changed` is true when a and b differ. */
export interface FieldDiff {
  field: string
  label: string
  a: unknown
  b: unknown
  changed: boolean
}

export interface SpecDiff {
  changed: Record<string, { a: unknown; b: unknown }>
  added: Record<string, unknown>
  removed: Record<string, unknown>
}

export interface FingerprintPair {
  a: string | null
  b: string | null
  changed: boolean
}

export interface StrategyComparison {
  version: { a: string | null; b: string | null }
  same_version: boolean
  /** Entry, exit, stop, filters, sessions, timing, portfolio and sizing intent. */
  rules: SpecDiff
  /** The tunable parameter values only. */
  parameters: SpecDiff
  fingerprints: { rules: FingerprintPair; params: FingerprintPair; strategy: FingerprintPair }
}

export interface DatasetComparison {
  fields: FieldDiff[]
  /** True when the dataset fingerprints differ: history was corrected or backfilled. */
  fingerprint_changed: boolean
}

export interface PeriodRange {
  start: string
  end: string
}

export interface Overlap {
  periods: { a: PeriodRange | null; b: PeriodRange | null }
  /** The sessions both runs cover. Null when the periods do not intersect. */
  common: PeriodRange | null
  /**
   * The parts of each run's period outside the common window, per run: disjoint inclusive
   * ranges in date order (up to two: before and after). Empty when none.
   */
  extra: { a: PeriodRange[]; b: PeriodRange[] }
  /** Plain-language warning naming each extra span, or null when the periods are identical. */
  warning: string | null
}

export interface PeriodViews {
  /** Each run's own full-period metrics, recomputed from its retained trades. */
  full: { a: RunMetrics; b: RunMetrics }
  /** Both runs restricted to the common sessions. Null when there is no common period. */
  common: { a: RunMetrics; b: RunMetrics } | null
  /**
   * The later-ending run's trades whose entry date lies in any of its own `ranges` (its extra
   * spans outside the common sessions). Null when there are none.
   */
  incremental: { run: 'a' | 'b'; ranges: PeriodRange[]; metrics: RunMetrics } | null
}

/** One x position (an ISO date or month) with each run's value. */
export interface CurvePoint {
  x: string
  a: number | null
  b: number | null
}

export interface DistributionBin {
  bin_start: number
  bin_end: number
  a: number
  b: number
}

export interface ComparisonCurves {
  equity_r: CurvePoint[]
  /** Equity minus running peak, zero or negative (zero at a new high). */
  drawdown_r: CurvePoint[]
  /** Net R per calendar month; x is YYYY-MM. */
  monthly: CurvePoint[]
  /** Rolling mean net R per trade over `rolling_window` trades. */
  rolling: CurvePoint[]
  rolling_window: number
  /** Net R per trade, on bins shared by both runs. */
  distribution: DistributionBin[]
  /** Marked on the charts: the shared sessions, or null when the periods do not intersect. */
  common: PeriodRange | null
  /** The later-ending run's extra spans (disjoint ranges); empty when there is no incremental view. */
  incremental: PeriodRange[]
}

export interface RunComparison {
  schema: 1
  /** Every component that differs, in a fixed order; ['RESEARCH_RERUN'] when nothing differs. */
  change_classes: string[]
  strategy: StrategyComparison
  scope: FieldDiff[]
  dataset: DatasetComparison
  execution: FieldDiff[]
  cost: FieldDiff[]
  /** Full-period metric deltas from the recomputed views. */
  performance: Record<string, MetricDelta>
  /** The same deltas for the common sessions only. Null when there is no common period. */
  performance_common: Record<string, MetricDelta> | null
  overlap: Overlap
  views: PeriodViews
  /** Full-period curves. */
  curves: ComparisonCurves
  /**
   * The same curves on only the trades entered inside the common window, each run's equity
   * starting from 0 at the window start and drawdown measured inside the window. Null when
   * there is no common window.
   */
  curves_common: ComparisonCurves | null
  /**
   * The same curves on only the incremental run's trades outside the common window; the other
   * run's values are null throughout and equity starts from 0 at the first incremental span.
   * Null when there is no incremental view.
   */
  curves_incremental: ComparisonCurves | null
}

export interface BacktestCompareResponse extends Envelope {
  idea_id?: string
  run_a?: RunRef
  run_b?: RunRef
  metric_deltas?: Record<string, MetricDelta>
  params?: ParamDiff
  n_trials?: { a: number; b: number }
  comparable?: boolean
  warnings?: string[]
  info?: string[]
  equity_r?: { a: SeriesPoint[]; b: SeriesPoint[] }
  /** Null when either run has no run folder (a legacy record): see `comparison_note`. */
  comparison?: RunComparison | null
  comparison_note?: string | null
}

async function unwrap<T extends Envelope>(
  promise: Promise<{ data: T }>,
  fallbackMessage: string
): Promise<T> {
  try {
    const res = await promise
    return res.data
  } catch (error) {
    // 400/404/500 reject in axios, but the backend still sent a JSON error body.
    const axiosError = error as { response?: { data?: T } }
    if (axiosError.response?.data) {
      return axiosError.response.data
    }
    return { status: 'error', message: fallbackMessage } as T
  }
}

export type ExportFormat = 'json' | 'metrics' | 'series' | 'trades'

export function exportUrl(ideaId: string, runId: string, format: ExportFormat): string {
  const base = webClient.defaults.baseURL ?? ''
  const path = `/api/backtest-results/${encodeURIComponent(ideaId)}/runs/${encodeURIComponent(runId)}/export`
  return `${base}${path}?format=${format}`
}

export const backtestResultsApi = {
  list: (): Promise<BacktestListResponse> =>
    unwrap(
      webClient.get<BacktestListResponse>('/api/backtest-results'),
      'Failed to load backtests'
    ),

  idea: (ideaId: string): Promise<BacktestIdeaResponse> =>
    unwrap(
      webClient.get<BacktestIdeaResponse>(`/api/backtest-results/${encodeURIComponent(ideaId)}`),
      'Failed to load backtest history'
    ),

  trades: (
    ideaId: string,
    runId: string,
    query: TradesQuery = {}
  ): Promise<BacktestTradesResponse> => {
    const params = new URLSearchParams()
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== null && value !== '') params.set(key, String(value))
    }
    const qs = params.toString()
    return unwrap(
      webClient.get<BacktestTradesResponse>(
        `/api/backtest-results/${encodeURIComponent(ideaId)}/runs/${encodeURIComponent(runId)}/trades${qs ? `?${qs}` : ''}`
      ),
      'Failed to load trades'
    )
  },

  lineage: (ideaId: string, runId: string, tradeId: string): Promise<BacktestLineageResponse> =>
    unwrap(
      webClient.get<BacktestLineageResponse>(
        `/api/backtest-results/${encodeURIComponent(ideaId)}/runs/${encodeURIComponent(runId)}/trades/${encodeURIComponent(tradeId)}/lineage`
      ),
      'Failed to load trade lineage'
    ),

  run: (ideaId: string, runId: string): Promise<BacktestRunDetailResponse> =>
    unwrap(
      webClient.get<BacktestRunDetailResponse>(
        `/api/backtest-results/${encodeURIComponent(ideaId)}/runs/${encodeURIComponent(runId)}`
      ),
      'Failed to load run detail'
    ),

  compare: (ideaId: string, runA: string, runB: string): Promise<BacktestCompareResponse> => {
    const params = new URLSearchParams({ a: runA, b: runB })
    return unwrap(
      webClient.get<BacktestCompareResponse>(
        `/api/backtest-results/${encodeURIComponent(ideaId)}/compare?${params.toString()}`
      ),
      'Failed to compare runs'
    )
  },
}
