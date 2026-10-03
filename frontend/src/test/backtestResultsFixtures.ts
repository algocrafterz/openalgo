import type { IdeaCard, LatestRun, RunRecord } from '@/api/backtestResults'

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
