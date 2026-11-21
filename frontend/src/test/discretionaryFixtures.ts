import type {
  BreakdownRow,
  CalendarRow,
  ClassificationBlock,
  ConsistencyBlock,
  CostsBlock,
  DataQuality,
  DiscretionaryPosition,
  DiscretionarySummary,
  DiscretionaryToday,
  DiscretionaryTrade,
  ExcursionPosition,
  ExcursionsResponse,
  PointsByInstrumentRow,
  ProvisionalBlock,
  ProvisionalTrade,
  RiskBlock,
  TodayClosedPosition,
  TradeManagementBlock,
  TradingBreakdowns,
  UnresolvedInRange,
} from '@/api/discretionaryPnl'

export function breakdownRow(key: string, extra: Partial<BreakdownRow> = {}): BreakdownRow {
  return {
    key,
    trades: 2,
    wins: 1,
    losses: 1,
    win_rate: 50,
    profit_factor: 1.5,
    expectancy: 100,
    avg_win: 300,
    avg_loss: 200,
    realized_gross: 200,
    charges: 20,
    charges_unavailable_trades: 0,
    net_of_charges: 180,
    avg_hold_minutes: 95,
    median_hold_minutes: 95,
    ...extra,
  }
}

export function trade(extra: Partial<DiscretionaryTrade> = {}): DiscretionaryTrade {
  return {
    id: 11,
    broker: 'flattrade',
    account: 'FT1',
    symbol: 'RELIANCE',
    exchange: 'NSE',
    product: 'MIS',
    direction: 'LONG',
    closed_quantity: 10,
    entry_price: 100,
    exit_price: 110,
    opened_at: '2026-11-16 09:20:00',
    closed_at: '2026-11-16 10:05:00',
    trade_date: '2026-11-16',
    overnight: false,
    hold_minutes: 45,
    realized_pnl: 100,
    charges: 12.5,
    charges_status: 'ok',
    net_pnl: 87.5,
    legs: 1,
    ...extra,
  }
}

export function position(extra: Partial<DiscretionaryPosition> = {}): DiscretionaryPosition {
  return {
    broker: 'flattrade',
    account: 'FT1',
    symbol: 'TCS',
    exchange: 'NSE',
    product: 'CNC',
    direction: 'LONG',
    open_qty: 5,
    avg_entry: 3000,
    opened_at: '2026-11-13 11:00:00',
    ltp: null,
    fetched_at: null,
    age_s: null,
    price_status: 'not_requested',
    unrealized: null,
    realized_so_far: 0,
    opening_unknown: false,
    ...extra,
  }
}

export function summary(extra: Partial<DiscretionarySummary> = {}): DiscretionarySummary {
  const trades = [
    trade(),
    trade({
      id: 12,
      symbol: 'INFY',
      direction: 'SHORT',
      realized_pnl: -40,
      charges: null,
      charges_status: 'unavailable',
      net_pnl: null,
      broker: 'zerodha',
      account: 'ZR1',
    }),
  ]
  return {
    period: { period: '30d', start: '2026-10-19', end: '2026-11-17', reliable_since: '2026-11-01' },
    metrics: {
      start_date: '2026-10-19',
      end_date: '2026-11-17',
      trades_count: 2,
      winning_trades: 1,
      losing_trades: 1,
      win_rate: 50,
      gross_profit: 100,
      gross_loss: 40,
      net_profit: 60,
      profit_factor: 2.5,
      expectancy: 30,
      avg_win: 100,
      avg_loss: 40,
      sharpe_ratio: null,
      sortino_ratio: null,
      return_pct: null,
      max_drawdown: -40,
      equity_curve: [
        { date: '2026-11-16', value: 100 },
        { date: '2026-11-17', value: 60 },
      ],
      withheld: {
        sharpe_ratio: 'no capital basis',
        sortino_ratio: 'no capital basis',
        return_pct: 'no capital basis',
      },
    },
    build_id: 'b1',
    realized_gross: 60,
    charges: 12.5,
    net: 47.5,
    realized_net_of_charges: 47.5,
    net_definition: 'net = realized_gross minus estimated charges',
    charges_unavailable_trades: 1,
    unrealized_total: 0,
    open_positions: 1,
    per_identity: [],
    sync_health: {
      last_successful_sync: '2026-11-17 09:30',
      runs_by_day: {},
      days_without_sync: [],
    },
    warnings: [
      '1 trade(s) have no charge estimate (instrument not modelled); net P&L counts their charges as 0 and is not complete.',
      'last successful sync: 2026-11-17 09:30 IST',
      'unrealized P&L not requested.',
    ],
    market_hours_source: 'calendar',
    withheld: {
      sharpe_ratio: 'no capital basis',
      sortino_ratio: 'no capital basis',
      return_pct: 'no capital basis',
      ratio_capital: 'no capital basis',
    },
    labels: {},
    quotes_requested: false,
    by_period: {
      daily: [
        {
          period: '2026-11-16',
          label: '2026-11-16',
          start: '2026-11-16',
          end: '2026-11-16',
          trades: 2,
          wins: 1,
          losses: 1,
          realized_gross: 60,
          charges: 12.5,
          charges_unavailable_trades: 1,
          net_of_charges: 47.5,
          net_charges_complete: false,
        },
      ],
      weekly: [
        {
          period: '2026-11-16',
          label: '2026-W47',
          start: '2026-11-16',
          end: '2026-11-22',
          trades: 2,
          wins: 1,
          losses: 1,
          realized_gross: 60,
          charges: 12.5,
          charges_unavailable_trades: 1,
          net_of_charges: 47.5,
          net_charges_complete: false,
        },
      ],
      monthly: [
        {
          period: '2026-11',
          label: '2026-11',
          start: '2026-11-01',
          end: '2026-11-30',
          trades: 2,
          wins: 1,
          losses: 1,
          realized_gross: 60,
          charges: 12.5,
          charges_unavailable_trades: 1,
          net_of_charges: 47.5,
          net_charges_complete: false,
        },
      ],
    },
    by_symbol: [breakdownRow('RELIANCE'), breakdownRow('INFY')],
    by_side: [breakdownRow('LONG'), breakdownRow('SHORT')],
    by_product: [breakdownRow('MIS')],
    by_holding: {
      intraday: breakdownRow('', { key: undefined }),
      overnight: breakdownRow('', { key: undefined, trades: 0 }),
    },
    hold: {
      overall: { count: 2, mean_minutes: 45, median_minutes: 45 },
      winners: { count: 1, mean_minutes: 30, median_minutes: 30 },
      losers: { count: 1, mean_minutes: 60, median_minutes: 60 },
    },
    trades,
    trades_total: 2,
    trades_truncated: 0,
    positions: [position()],
    positions_total: 1,
    positions_truncated: 0,
    ...extra,
  }
}

export function emptySummary(): DiscretionarySummary {
  return summary({
    trades: [],
    trades_total: 0,
    positions: [],
    positions_total: 0,
    open_positions: 0,
    realized_gross: 0,
    charges: 0,
    net: 0,
    realized_net_of_charges: 0,
    charges_unavailable_trades: 0,
    warnings: [],
    // nothing closed: no period rows either (the backend sends none without a closing leg)
    by_period: { daily: [], weekly: [], monthly: [] },
  })
}

export function todayClosed(extra: Partial<TodayClosedPosition> = {}): TodayClosedPosition {
  return {
    id: 11,
    build_id: 'b1',
    broker: 'flattrade',
    account: 'A1',
    symbol: 'SBIN',
    exchange: 'NSE',
    product: 'MIS',
    direction: 'LONG',
    total_quantity: 45,
    entry_price: 100,
    exit_price: 110,
    opened_at: '2026-11-17 09:30:00',
    opened_session: '2026-11-17',
    closed_at: '2026-11-17 10:15:00',
    gross_pnl: 450,
    realized_today: 450,
    charges: 12.5,
    charges_status: 'ok',
    net_pnl: 437.5,
    legs: 1,
    status: 'Closed',
    ...extra,
  }
}

/** A `today` block with nothing in it (no closes, no open position, nothing pending). */
export function emptyToday(extra: Partial<DiscretionaryToday> = {}): DiscretionaryToday {
  return {
    session_date: '2026-11-17',
    realized_gross: 0,
    charges: 0,
    charges_unavailable_legs: 0,
    charges_complete: true,
    net: 0,
    realized_net_of_charges: 0,
    exit_legs: 0,
    unrealized: null,
    unrealized_unavailable_positions: 0,
    price_statuses: {},
    quotes_requested: false,
    completed_positions: 0,
    open_positions: 0,
    partially_closed_positions: 0,
    metrics: { trades_count: 0, winning_trades: 0, losing_trades: 0 },
    closed_positions: [],
    open_position_rows: [],
    pending_attribution: {
      grace_minutes: 30,
      last_evaluated_at: '2026-11-17 10:55:00',
      grace_count: 0,
      earliest_attributable_at: null,
      grace_fills: [],
      other_today: 0,
      other_total: 0,
    },
    ...extra,
  }
}

/** Risk strip of the default summary: RELIANCE +100 (id 11) and INFY -40 (id 12), 2 days. */
export function consistency(extra: Partial<ConsistencyBlock> = {}): ConsistencyBlock {
  const ref = (t: DiscretionaryTrade) => ({
    id: t.id,
    build_id: 'b1',
    broker: t.broker,
    account: t.account,
    symbol: t.symbol,
    exchange: t.exchange,
    product: t.product,
    direction: t.direction,
    realized_pnl: t.realized_pnl,
    charges: t.charges,
    charges_status: t.charges_status,
    net_pnl: t.net_pnl,
    closed_at: t.closed_at,
    trade_date: t.trade_date,
  })
  return {
    completed_trades: 2,
    largest_win: ref(trade()),
    largest_loss: ref(trade({ id: 12, symbol: 'INFY', realized_pnl: -40, net_pnl: null })),
    max_consecutive_wins: 1,
    max_consecutive_losses: 1,
    streak_rule:
      'completed trades ordered by close time (ties by id); a break-even trade ends both streaks',
    trading_days: 2,
    profitable_days: 1,
    losing_days: 1,
    flat_days: 0,
    avg_daily_pnl: 30,
    avg_daily_net: 23.75,
    avg_daily_net_complete: false,
    avg_daily_unavailable_reason: null,
    max_drawdown: -40,
    max_drawdown_unavailable_reason: null,
    avg_hold_minutes: 45,
    median_hold_minutes: 45,
    ...extra,
  }
}

export function tradingBreakdowns(extra: Partial<TradingBreakdowns> = {}): TradingBreakdowns {
  return {
    time_of_day: [
      breakdownRow('09:15-10:00', { trading_days: 1, sample_ok: false }),
      breakdownRow('other', { trades: 1, trading_days: 1, sample_ok: false }),
    ],
    weekday: [breakdownRow('Mon', { trading_days: 1, sample_ok: true, profit_factor: 1.5 })],
    hold_bucket: [
      breakdownRow('15-60m', { trading_days: 1, sample_ok: false }),
      breakdownRow('overnight', { trades: 0, trading_days: 0, sample_ok: false }),
    ],
    ...extra,
  }
}

export function dataQuality(extra: Partial<DataQuality> = {}): DataQuality {
  return {
    collected: 9,
    awaiting_attribution: 2,
    awaiting_grace: 1,
    awaiting_other: 1,
    grace_minutes: 30,
    earliest_attributable_at: '2026-11-17 10:35:00',
    attributed_open: 3,
    included: 3,
    not_placed: 1,
    reconciles: true,
    charges_incomplete_legs: 1,
    charges_incomplete_trades: 1,
    prices: { requested: false, open_positions: 1, statuses: {}, missing: 0, stale: 0 },
    sync: { last_successful_sync: '2026-11-17 09:30', days_without_sync: [], problems: [] },
    ...extra,
  }
}

/** The real TCS round trip of 2026-11-18 (five unresolved fills), as the server replays it. */
export function provisionalTcs(extra: Partial<ProvisionalTrade> = {}): ProvisionalTrade {
  return {
    id: 1,
    provisional: true,
    broker: 'flattrade',
    account: 'FZ***',
    symbol: 'TCS',
    exchange: 'NSE',
    product: 'MIS',
    direction: 'LONG',
    closed_quantity: 45,
    entry_price: 2396.4089,
    exit_price: 2395.2,
    opened_at: '2026-11-18 09:46:18',
    closed_at: '2026-11-18 10:16:38',
    trade_date: '2026-11-18',
    overnight: false,
    hold_minutes: 30.33,
    realized_pnl: -54.4,
    charges: 38.25,
    charges_status: 'ok',
    net_pnl: -92.65,
    legs: 2,
    opening_unknown: false,
    fills: [
      {
        fill_ref: 1,
        role: 'ENTRY',
        side: 'BUY',
        qty_used: 4,
        price: 2392.6,
        fill_time: '2026-11-18 09:46:18',
        charge_allocated: 2,
      },
      {
        fill_ref: 2,
        role: 'ENTRY',
        side: 'BUY',
        qty_used: 21,
        price: 2392,
        fill_time: '2026-11-18 09:46:18',
        charge_allocated: 10,
      },
      {
        fill_ref: 3,
        role: 'ENTRY',
        side: 'BUY',
        qty_used: 20,
        price: 2401.8,
        fill_time: '2026-11-18 10:07:49',
        charge_allocated: 9,
      },
      {
        fill_ref: 4,
        role: 'EXIT',
        side: 'SELL',
        qty_used: 30,
        price: 2395.2,
        fill_time: '2026-11-18 10:16:38',
        charge_allocated: 11.5,
      },
      {
        fill_ref: 5,
        role: 'EXIT',
        side: 'SELL',
        qty_used: 15,
        price: 2395.2,
        fill_time: '2026-11-18 10:16:38',
        charge_allocated: 5.75,
      },
    ],
    ...extra,
  }
}

export function provisional(extra: Partial<ProvisionalBlock> = {}): ProvisionalBlock {
  return {
    fills: 5,
    trades: [provisionalTcs()],
    open_positions: [],
    orphans: [],
    metrics: {
      trades: 1,
      wins: 0,
      losses: 1,
      win_rate: 0,
      profit_factor: 0,
      expectancy: -54.4,
      avg_win: 0,
      avg_loss: 54.4,
      realized_gross: -54.4,
      charges: 38.25,
      net_of_charges: -92.65,
      charges_unavailable_trades: 0,
      net_charges_complete: true,
    },
    sample: {
      completed_trades: 1,
      trading_days: 1,
      min_trades: 30,
      min_days: 10,
      sufficient: false,
    },
    ...extra,
  }
}

// ---------------------------------------------------------------- phase 2 blocks

export function classification(extra: Partial<ClassificationBlock> = {}): ClassificationBlock {
  return {
    selected: 'all',
    closed: {
      intraday: 1,
      swing: 0,
      unclassified: 1,
      unclassified_reasons: { opening_unknown: 1 },
    },
    open: { intraday: 0, swing: 1, unclassified: 0, unclassified_reasons: {} },
    note: 'intraday = opened and closed in the same exchange session; swing = still open after the opening session ended; unclassified (reason shown) is never guessed and is in the all view only.',
    ...extra,
  }
}

export function unresolvedInRange(extra: Partial<UnresolvedInRange> = {}): UnresolvedInRange {
  return {
    unresolved_fills: 2,
    orphan_fills: 1,
    total: 3,
    start: '2026-10-19',
    end: '2026-11-17',
    ...extra,
  }
}

export function pointsRow(extra: Partial<PointsByInstrumentRow> = {}): PointsByInstrumentRow {
  return {
    symbol: 'RELIANCE',
    exchange: 'NSE',
    positions: 3,
    winners: 2,
    losers: 1,
    gross_points_gained: 25,
    gross_points_lost: 8,
    net_points: 17,
    avg_points_per_position: 5.6667,
    avg_win_points: 12.5,
    avg_loss_points: -8,
    largest_win_points: 15,
    largest_loss_points: -8,
    ...extra,
  }
}

export function riskBlock(extra: Partial<RiskBlock> = {}): RiskBlock {
  return {
    basis: 'net realised equity by position close time, peak starts at 0',
    positions: 5,
    net_unavailable_positions: 1,
    max_drawdown: 320.5,
    current_drawdown: 120,
    peak_equity: 900,
    latest_equity: 780,
    max_drawdown_at: '2026-11-12 14:10:00',
    max_drawdown_pct: null,
    current_drawdown_pct: null,
    percent_unavailable_reason: 'no capital basis',
    max_consecutive_wins: 3,
    max_consecutive_losses: 2,
    current_streak: { type: 'loss', length: 2 },
    ...extra,
  }
}

export function calendarRow(key: string, extra: Partial<CalendarRow> = {}): CalendarRow {
  return {
    key,
    positions: 4,
    wins: 3,
    losses: 1,
    win_rate: 75,
    realized_gross: 500,
    charges: 40,
    net_of_charges: 460,
    charges_unavailable_positions: 0,
    net_unavailable_positions: 0,
    ...extra,
  }
}

export function monthRows(): CalendarRow[] {
  return [
    { period: '2026-10', ...calendarRow('2026-10') },
    {
      period: '2026-11',
      ...calendarRow('2026-11', { net_of_charges: -150, realized_gross: -120 }),
    },
  ]
}

export function weekdayRows(): CalendarRow[] {
  return [
    { weekday: 'Mon', ...calendarRow('Mon') },
    {
      weekday: 'Tue',
      ...calendarRow('Tue', {
        positions: 0,
        wins: 0,
        losses: 0,
        win_rate: null,
        realized_gross: 0,
        charges: 0,
        net_of_charges: 0,
      }),
    },
    { weekday: 'Wed', ...calendarRow('Wed', { net_of_charges: -75, realized_gross: -60 }) },
  ]
}

export function tradeManagement(extra: Partial<TradeManagementBlock> = {}): TradeManagementBlock {
  return {
    net_basis_positions: 5,
    net_unavailable_positions: 1,
    break_even_excluded: 1,
    median_net_pnl: 42.5,
    avg_net_pnl: 30,
    winners: { count: 3, mean_minutes: 45, median_minutes: 30 },
    losers: { count: 1, mean_minutes: 125, median_minutes: 125 },
    ...extra,
  }
}

export function costsBlock(extra: Partial<CostsBlock> = {}): CostsBlock {
  return {
    total_charges: 120,
    gross_profit: 800,
    charges_pct_of_gross_profit: 15,
    reason: null,
    charges_unavailable_positions: 0,
    ...extra,
  }
}

export function excursionPosition(extra: Partial<ExcursionPosition> = {}): ExcursionPosition {
  return {
    id: 11,
    symbol: 'RELIANCE',
    exchange: 'NSE',
    direction: 'LONG',
    category: 'intraday',
    opened_at: '2026-11-16 09:20:00',
    closed_at: '2026-11-16 10:05:00',
    closed_quantity: 10,
    points_per_unit: 10,
    net_pnl: 87.5,
    status: 'available',
    reason: null,
    reason_text: null,
    mae_points: 2.5,
    mfe_points: 14,
    mae_amount: 25,
    mfe_amount: 140,
    minute_bar_precision: true,
    profit_capture: 0.7143,
    ...extra,
  }
}

export const EXCURSION_NOTE =
  'MAE/MFE for completed trades becomes available after market close, once price data processing is complete.'

export function excursions(extra: Partial<ExcursionsResponse> = {}): ExcursionsResponse {
  const dist = (average: number | null, median: number | null, count = 1) => ({
    count,
    average,
    median,
  })
  return {
    build_id: 'b1',
    period: { period: '30d', start: '2026-10-19', end: '2026-11-17' },
    category: 'all',
    positions: [
      excursionPosition(),
      excursionPosition({
        id: 12,
        symbol: 'INFY',
        direction: 'SHORT',
        net_pnl: -40,
        status: 'unavailable',
        reason: 'unsupported_segment',
        reason_text: 'unsupported segment',
        mae_points: null,
        mfe_points: null,
        mae_amount: null,
        mfe_amount: null,
        profit_capture: null,
      }),
    ],
    positions_total: 2,
    computed: 1,
    cap: 300,
    aggregates: {
      winners: {
        mae_points: dist(2.5, 2.5),
        mfe_points: dist(14, 14),
        mae_amount: dist(25, 25),
        mfe_amount: dist(140, 140),
        profit_capture: dist(0.7143, 0.7143),
      },
      losers: {
        mae_points: dist(null, null, 0),
        mfe_points: dist(null, null, 0),
        mae_amount: dist(null, null, 0),
        mfe_amount: dist(null, null, 0),
      },
      coverage: {
        available: 1,
        total: 2,
        label: '1 of 2 positions available',
        unavailable_reasons: { unsupported_segment: 1 },
      },
      minute_bar_precision: true,
      precision_note:
        'Minute-bar based lower bound of the true excursion, not tick accurate: the minute of each fill is observed only at the fill price.',
    },
    note: EXCURSION_NOTE,
    ...extra,
  }
}
