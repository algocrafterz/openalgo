import type {
  EngineBlocks,
  ExecutionWarnings,
  LookaheadGuard,
  SessionCoverage,
} from '@/api/backtestResults'

export const WARNINGS: ExecutionWarnings = {
  trades: 200,
  ambiguous_exit_trades: 12,
  ambiguous_exit_share: 0.06,
  data_gap_trades: 4,
  data_gap_share: 0.02,
  entries_skipped_missing_bar: 3,
  ambiguous_sensitivity: {
    label: 'UPPER BOUND, NOT A RESULT',
    trades: 12,
    actual_mean_net_r: -0.45,
    upper_bound_mean_net_r: 0.8,
    run_mean_net_r_actual: 0.1,
    run_mean_net_r_upper_bound: 0.17,
    target_basis: 'Trade.tp, the target the signal carried',
    exact: true,
  },
}

export const CLEAN_WARNINGS: ExecutionWarnings = {
  ...WARNINGS,
  ambiguous_exit_trades: 0,
  ambiguous_exit_share: 0,
  data_gap_trades: 0,
  data_gap_share: 0,
  ambiguous_sensitivity: {
    actual_mean_net_r: null,
    upper_bound_mean_net_r: null,
    exact: true,
  },
}

export const GUARD: LookaheadGuard = {
  mode: 'full',
  symbols_checked: 40,
  sessions_checked: 52000,
  violations: 0,
  lookahead_check: 'run',
  truncation: { status: 'run', ok: true, mismatches: 0 },
}

export const COVERAGE: SessionCoverage = {
  coverage_class: 'MATERIAL_GAPS',
  disclosure:
    'Some trading sessions are missing from the stored history for part of this basket; results may be affected.',
  session_basis: { used: 'common', source: 'rule', symbols: 3 },
  effective: {
    period: { start: '2021-01-04', end: '2026-09-25' },
    sessions_kept: 1380,
    sessions_excluded_by_basis: { count: 9, ranges: ['2021-02-01 to 2021-02-09'] },
  },
  universe: {
    symbols: 3,
    symbols_complete: 1,
    symbols_minor_gaps: 1,
    symbols_material_gaps: 1,
    symbols_severe_gaps: 0,
    expected_sessions_after_exclusions_total: 4200,
    missing_full_sessions_total: 40,
    partial_sessions_total: 6,
    missing_bars_total: 55,
  },
  symbols: [
    {
      symbol: 'AAA',
      class: 'COMPLETE',
      calendar_sessions: 1400,
      loaded_sessions: 1400,
      coverage_share: 1,
      expected_sessions_after_exclusions: 1400,
      missing_share: 0,
      missing_full_sessions: { count: 0, ranges: [], share: 0 },
      partial_sessions: { audited: true, count: 0, missing_bars: 0 },
    },
    {
      symbol: 'BBB',
      class: 'MATERIAL_GAPS',
      calendar_sessions: 1400,
      loaded_sessions: 1360,
      coverage_share: 0.9714,
      expected_sessions_after_exclusions: 1400,
      missing_share: 0.0286,
      missing_full_sessions: {
        count: 40,
        ranges: ['2022-03-01 to 2022-03-14', '2023-05-02'],
        share: 0.0286,
      },
      partial_sessions: { audited: true, count: 6, missing_bars: 55 },
    },
    {
      symbol: 'CCC',
      class: 'MINOR_GAPS',
      calendar_sessions: 1400,
      loaded_sessions: 1399,
      coverage_share: 0.9993,
      expected_sessions_after_exclusions: 1400,
      missing_share: 0.0007,
      missing_full_sessions: { count: 1, ranges: ['2024-01-01'], share: 0.0007 },
      partial_sessions: { audited: false, count: null, missing_bars: null },
    },
  ],
  symbols_not_listed: 2,
  continuity_note:
    'Only previous-session context is guarded: the first loaded session after a missing official session takes no new entries (SESSION_AFTER_GAP). Rolling indicators are not reset at a gap.',
}

export const ENGINE_BLOCKS: EngineBlocks = {
  execution_warnings: WARNINGS,
  lookahead_guard: GUARD,
  session_coverage: COVERAGE,
  cost_model_version: 2,
  engine_version: '1.6',
}
