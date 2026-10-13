import type { GateRow, IdeaCard, LatestRun, RunDetail, RunRecord } from '@/api/backtestResults'

export function makeLatest(overrides: Partial<LatestRun> = {}): LatestRun {
  return {
    run_id: '2026-09-30-x-v1',
    date: '2026-09-30',
    code_version: 'abc1234',
    parent_run_id: null,
    change_note: '',
    metrics: { trades: 1200, gross_bps: 8.1, net_bps: -1.3, t_stat: 1.9, hurdle_t: 1.97 },
    n_trials: 1,
    promoted: false,
    oos_net_bps: -7.4,
    net_bps_cap10: null,
    baseline_excess_bps: null,
    period: { start: '2016-10-03', end: '2026-09-25' },
    data: {
      source: 'historify',
      as_of: '2026-09-29',
      universe: 'historify-208-symbols',
      bar_size: '5m',
    },
    years: 10,
    data_age_days: 0,
    sparkline: [0, 1, 2, 1.5, 3],
    ...overrides,
  }
}

export function makeCard(overrides: Partial<IdeaCard> = {}): IdeaCard {
  return {
    id: 'IDEA-001',
    title: 'Sample idea',
    status: 'rejected',
    verdict: 'no-edge',
    horizon: 'intraday',
    summary: 'One line summary.',
    run_count: 1,
    latest_run: makeLatest(),
    ...overrides,
  }
}

export function makeRun(overrides: Partial<RunRecord> = {}): RunRecord {
  return {
    run_id: '2026-09-30-x-v1',
    date: '2026-09-30',
    code_version: 'abc1234',
    data: { source: 'historify', as_of: '2026-09-29', universe: 'fno-208', bar_size: '5m' },
    period: { start: '2021-01-01', end: '2026-09-25' },
    costs: { model: 'zerodha-mis', round_trip_bps: 9.4 },
    metrics: {
      trades: 1200,
      gross_bps: 8.1,
      net_bps: -1.3,
      win_rate: 0.41,
      profit_factor: 0.95,
      expectancy_r: -0.02,
      sharpe: 0.4,
      t_stat: 1.9,
      hurdle_t: 1.97,
      max_dd_r: 12.5,
      days: 800,
      breakeven_cost_bps: 8.1,
    },
    params: { or_minutes: 15 },
    parent_run_id: null,
    change_note: 'first baseline',
    n_trials: 1,
    promoted: false,
    cost_sensitivity: [
      { round_trip_bps: 0, net_bps: 8.1 },
      { round_trip_bps: 9.4, net_bps: -1.3 },
    ],
    series: {
      equity_r: [
        ['2026-01-02', 1],
        ['2026-01-05', 0.5],
      ],
      drawdown_r: [
        ['2026-01-02', 0],
        ['2026-01-05', -0.5],
      ],
      monthly: [
        { month: '2026-01', n: 2, total_r: 0.5, net_r: 0.25 },
        { month: '2026-03', n: 3, total_r: -1.2, net_r: -0.4 },
      ],
    },
    ...overrides,
  }
}

/** A captured run detail with every field present and empty. */
export function makeRunDetail(overrides: Partial<RunDetail> = {}): RunDetail {
  return {
    run_id: '2026-09-30-x-v1',
    schema: 3,
    captured: true,
    strategy_version: 'v1',
    parent_version: null,
    branch_label: null,
    change_classes: [],
    verdict: null,
    promotion: null,
    next_action: null,
    spec: null,
    fingerprints: null,
    scope: null,
    reproducibility: null,
    assumptions: null,
    missing_capabilities: [],
    manifest_sha256: null,
    created_ist: null,
    validity: null,
    data_quality: null,
    trials: null,
    ledgers: null,
    cost_components: null,
    has_trades: false,
    has_ledgers: false,
    ...overrides,
  }
}

function gate(
  g: string,
  title: string,
  status: GateRow['status'],
  evidence: string,
  hard = false
): GateRow {
  return { gate: g, title, status, hard, value: null, threshold: 't', evidence }
}

/** An IDEA-028-shaped no-stop swing run: profitable, MARGINAL, behind its benchmarks. */
export function makeSwingRun(overrides: Partial<RunRecord> = {}): RunRecord {
  return makeRun({
    run_id: '2026-10-05-low-volatility-v1',
    costs: { model: 'flat-round-trip-bps', round_trip_bps: 24 },
    metrics: {
      trades: 301,
      gross_bps: 1634.358,
      net_bps: 1610.358,
      win_rate: 0.6512,
      t_stat: 5.657,
      sharpe: 4.863,
      days: 114,
      hurdle_t: 1.972,
      profit_factor: 5.412,
    },
    cost_model: { mean_total_bps: 30.248, net_bps: 1604.1099, flat_bps: 24 },
    cost_sensitivity: [
      { round_trip_bps: 0, net_bps: 1634.358 },
      { round_trip_bps: 24, net_bps: 1610.358 },
    ],
    baseline: {
      gross_bps: 1800.18,
      net_bps: 1776.18,
      trade_net_bps: 1467.03,
      excess_net_bps: -309.14,
      excess_t_stat: -1.805,
      trades: 299,
    },
    split: { type: 'oos', oos_start: '2023-07-13' },
    in_sample: { trades: 179, net_bps: 1333.22 } as RunRecord['in_sample'],
    out_of_sample: { trades: 92, net_bps: 476.86 } as RunRecord['out_of_sample'],
    run_verdict: 'MARGINAL',
    evaluation: {
      version: 1,
      dimensions: [
        {
          name: 'stability',
          rows: [
            gate('G3.2', 'Out-of-sample result', 'fail', 'OOS +476.9 bps vs in-sample +2109.3 bps'),
            gate('G3.3', 'Walk-forward', 'fail', '0 of 19 windows net positive (0 empty)'),
            gate('G3.4', 'Symbol hold-out', 'pass', 'half A +1633.7 bps, half B +1586.3 bps'),
          ],
        },
        {
          name: 'statistical_confidence',
          rows: [
            gate('G2.2', 'Day-clustered t-statistic', 'pass', 'day-clustered t 5.66', true),
            gate('G4.1', 'Parameter perturbation', 'not_evaluated', 'needs a sweep', true),
            gate(
              'G4.7',
              'Survivorship',
              'report',
              'survivorship bias True. Results are biased toward survivors'
            ),
          ],
        },
      ],
      summary: { counts: { pass: 2, fail: 2 }, hard_failed: [] },
    },
    ...overrides,
  })
}

export function makeSwingDetail(overrides: Partial<RunDetail> = {}): RunDetail {
  const recorded = (status: GateRow['status'], evidence: string) => ({
    status,
    value: 0,
    threshold: 't',
    evidence,
  })
  return makeRunDetail({
    run_id: '2026-10-05-low-volatility-v1',
    verdict: {
      run_verdict: 'MARGINAL',
      primary_failed_gate: 'G3.2',
      reasons: [],
      caps_applied: [],
    },
    promotion: {
      stages: {
        backtest: { decision: 'NOT_READY', gates: [], reasons: ['gate G3.2 failed'] },
        paper: { decision: 'BLOCKED', gates: [], reasons: [] },
        live: { decision: 'BLOCKED', gates: [], reasons: [] },
      },
      next_stage: null,
    },
    next_action: {
      action: 'Verify that backtest and live share one code path',
      reason: 'Divergent logic makes results unreliable',
      gate: 'G0.1',
    },
    evaluations: {
      corrections: [
        {
          gate: 'G3.2',
          recorded: recorded('fail', 'OOS +476.9 bps vs in-sample +2109.3 bps (209 trades)'),
          corrected: recorded('fail', 'OOS +476.9 bps vs in-sample +1333.2 bps (179 trades)'),
          changed_status: false,
          file: '20261014T005227-ist-session-gates.json',
          evaluated_ist: '2026-10-14T00:52:27+05:30',
          reason: 'purged in-sample',
        },
        {
          gate: 'G3.3',
          recorded: recorded('fail', '0 of 19 windows net positive (0 empty)'),
          corrected: recorded('pass', '17 of 19 windows net positive (0 empty)'),
          changed_status: true,
          file: '20261005T144832-ist-g33.json',
          evaluated_ist: '2026-10-05T14:48:32+05:30',
          reason: 'walk-forward averaged an undefined R',
        },
      ],
      portfolio: {
        source: 'reconstructed',
        capital_rs: 3000000,
        notional_rs: 100000,
        slots: 30,
        cost_column: 'cost_bps',
        stats: {
          sessions: 2396,
          sessions_with_pnl: 2396,
          years: 9.508,
          total_return_pct: 161.57,
          annual_return_pct: 10.64,
          annual_volatility_pct: 9.84,
          sharpe: 1.08,
          max_drawdown_pct: -22.97,
          max_drawdown_peak: '2020-01-17',
          max_drawdown_trough: '2020-03-23',
          first_session: '2017-02-01',
          last_session: '2026-10-01',
        },
        alternate_cost: {
          cost_column: 'modeled_cost_bps',
          stats: { annual_return_pct: 10.61, max_drawdown_pct: -23.01, total_return_pct: 160.95 },
        },
        equity_pct: [
          ['2017-02-01', 0],
          ['2026-10-01', 161.57],
        ],
        drawdown_pct: [
          ['2017-02-01', 0],
          ['2020-03-23', -22.97],
        ],
        monthly_pct: [{ month: '2017-02', return_pct: 1.2 }],
        session_counts: { recorded_days: 114, entry_sessions: 114, test_sessions: 2396 },
        evaluated_ist: '2026-10-14T00:52:27+05:30',
      },
      diagnostics: [
        {
          name: 'equal_weight_eligible_universe',
          title: 'Equal-weight eligible universe',
          reproducible: false,
          strategy: {
            annual_return_pct: 14.6,
            annual_volatility_pct: 13.6,
            max_drawdown_pct: -19,
            return_per_volatility: 1.07,
          },
          benchmark: {
            annual_return_pct: 20.8,
            annual_volatility_pct: 19.4,
            max_drawdown_pct: -32,
            return_per_volatility: 1.07,
          },
        },
      ],
    },
    ...overrides,
  })
}
