import type { TradeSubset } from '@/api/backtestResults'

/**
 * Which trade subset really backs a promotion gate. Exact gate ids only, no prefix matching:
 * G3.3 (walk-forward) and G3.4 (symbol hold-out) are not the date-based out-of-sample subset.
 */
export const GATE_TRADE_SUBSETS: readonly { gate: string; subset: TradeSubset }[] = [
  { gate: 'G1.2', subset: 'all' },
  { gate: 'G1.4', subset: 'all' },
  { gate: 'G2.1', subset: 'all' },
  { gate: 'G3.1', subset: 'oos' },
  { gate: 'G3.2', subset: 'oos' },
  { gate: 'G6.5', subset: 'gap_exits' },
]

/** Trade-row columns that are modelled cost components (basis points). Other numerics are not. */
export const COST_COMPONENT_KEYS: readonly string[] = [
  'cost_brokerage_bps',
  'cost_stt_bps',
  'cost_exchange_bps',
  'cost_gst_bps',
  'cost_sebi_bps',
  'cost_stamp_bps',
  'cost_slippage_bps',
  'modeled_cost_bps',
]

export function tradeSubsetForGate(gate: string | null | undefined): TradeSubset | null {
  if (!gate) return null
  const hit = GATE_TRADE_SUBSETS.find((g) => gate === g.gate)
  return hit ? hit.subset : null
}

export const TRADE_SUBSET_LABELS: Record<TradeSubset, string> = {
  all: 'All trades',
  in_sample: 'In-sample',
  oos: 'Out-of-sample',
  losers: 'Losing trades',
  gap_exits: 'Gap exits',
}

export const TRADE_SUBSETS = Object.keys(TRADE_SUBSET_LABELS) as TradeSubset[]

/** Plain-language labels for the engine's exclusion reason codes. */
export const EXCLUSION_LABELS: Record<string, string> = {
  MAX_TRADES_DAY: 'Daily trade limit reached',
  OPEN_WINDOW: 'Inside the opening window',
  ENTRY_CUTOFF: 'After the entry cutoff',
  DIRECTION_DISABLED: 'Trade direction switched off',
  DIRECTION_ALREADY_TAKEN: 'Direction already taken that day',
  RISK_INVALID: 'Risk could not be computed',
  STOP_WRONG_SIDE: 'Stop on the wrong side of entry',
  STOP_TOO_TIGHT: 'Stop tighter than the minimum',
  POSITION_OPEN: 'A position was already open',
  NO_NEXT_BAR: 'No next bar to fill on',
  ORDER_DROPPED_SESSION_END: 'Order dropped at session end',
  ORDER_DROPPED_POSITION_OPEN: 'Order dropped, position already open',
  POSITION_CAP: 'Position cap reached',
  SYMBOL_MIN_SESSIONS: 'Symbol below the minimum sessions',
  SPLIT_DAY: 'Split day excluded',
  BAD_BAR: 'Bad bar data',
  NOT_ELIGIBLE: 'Symbol not eligible on the day',
  WARMUP: 'Inside the indicator warm-up period',
  UNKNOWN_REASON: 'Reason not recorded',
}

export function exclusionLabel(code: string): string {
  return EXCLUSION_LABELS[code] ?? code
}

export function sortedExclusions(
  byReason: Record<string, number> | null | undefined
): { code: string; label: string; count: number }[] {
  return Object.entries(byReason ?? {})
    .map(([code, count]) => ({ code, label: exclusionLabel(code), count }))
    .sort((a, b) => b.count - a.count || a.code.localeCompare(b.code))
}
