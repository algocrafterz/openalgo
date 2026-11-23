import { useState } from 'react'
import type {
  BreakdownRow,
  ConsistencyBlock,
  PeriodRow,
  RiskBlock,
  SampleContext,
  TradeRef,
  TradingBreakdowns,
} from '@/api/discretionaryPnl'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { dailyTimeline } from '@/lib/discretionaryEquityRange'
import {
  bucketLabel,
  DAILY_CHART_CAPTION,
  dailyBars,
  drawdownRecoveryText,
  fmtNetMoney,
  fmtWinRate,
  HOW_YOU_TRADE_OPTIONS,
  type HowYouTradeKey,
  INSUFFICIENT_DATA,
  NET_DRAWDOWN_BASIS,
  netIncomplete,
  pnlTextClass,
  quantityText,
  reentryText,
  rowProfitFactor,
  sampleNote,
  streakText,
  timeOfDayBars,
  tradesPerDayText,
  winningDaysText,
} from '@/lib/discretionaryPnlView'
import { fmtDuration } from '@/lib/strategyPnlView'
import { cn } from '@/lib/utils'
import { NetBarChart } from './DiscretionaryCharts'

type Money = (value: number) => string

/**
 * In-page link that scrolls to a section and keeps the page URL (and its filters) as it is.
 * `beforeJump` runs first for targets inside a collapsed section; the jump then waits for the
 * target to render.
 */
export function JumpLink({
  to,
  children,
  beforeJump,
}: {
  to: string
  children: React.ReactNode
  beforeJump?: () => void
}) {
  const go = (tries: number) => {
    const el = document.getElementById(to)
    if (!el) {
      if (tries > 0) requestAnimationFrame(() => go(tries - 1))
      return
    }
    el.scrollIntoView({ behavior: 'smooth', block: 'start' })
    el.focus?.({ preventScroll: true })
  }
  return (
    <a
      className="underline underline-offset-2"
      href={`#${to}`}
      onClick={(e) => {
        if (!beforeJump && !document.getElementById(to)) return
        e.preventDefault()
        beforeJump?.()
        go(beforeJump ? 30 : 0)
      }}
    >
      {children}
    </a>
  )
}

/** "Ratios based on N completed trades" under the metric cards; nothing when there are none. */
export function SampleNote({ sample }: { sample: SampleContext | undefined }) {
  const note = sampleNote(sample)
  if (!note) return null
  return (
    <p className="text-xs text-muted-foreground" data-testid="discretionary-sample-note">
      {note.text}
    </p>
  )
}

/**
 * Daily realized net P&L bars (after estimated charges, the equity curve's basis) from the server's daily rows, with
 * the profitable / losing day count. Plain bars: every bar has a tooltip with charges and net.
 */
export function DailyPnlChart({
  rows,
  formatCurrency,
  notCollectedDays,
}: {
  rows: PeriodRow[]
  formatCurrency: Money
  /** Weekdays whose broker trades were not collected: shown as a flagged gap, never as zero. */
  notCollectedDays?: string[]
}) {
  const { bars, profitable, losing, flat } = dailyBars(rows)
  // One day is not a comparison; the daily figure is in the header and the calendar.
  if (bars.length < 2) return null
  const barByDate = new Map(bars.map((b) => [b.date, b]))
  // One slot per day with a row and per weekday without one: a day without a row is an empty
  // slot (no bar), not a bar of zero.
  const slots = dailyTimeline(rows, notCollectedDays)
  return (
    <div data-testid="daily-pnl-chart">
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <div className="text-sm font-medium">Daily net P&amp;L</div>
        <div className="text-xs text-muted-foreground" data-testid="daily-pnl-days">
          <span className="text-green-700 dark:text-green-600 [.analyzer_&]:text-green-600">
            {profitable} profitable
          </span>
          {' / '}
          <span className="text-red-600 dark:text-red-400">{losing} losing</span>
          {flat > 0 ? ` / ${flat} flat` : ''} day(s) (net)
        </div>
      </div>
      <div className="overflow-x-auto">
        <div
          className="flex h-40 items-stretch gap-1 border-y border-dashed border-border px-1"
          style={{ minWidth: `${slots.length * 12}px` }}
          role="img"
          aria-label={`Daily net P&L over ${bars.length} trading day(s): ${profitable} profitable, ${losing} losing`}
        >
          {slots.map((slot) => {
            const b = barByDate.get(slot.date)
            if (!b) {
              return (
                <div
                  key={slot.date}
                  className={cn(
                    'flex min-w-[8px] max-w-[40px] flex-1 flex-col justify-end',
                    slot.notCollected && 'border-b-2 border-dashed border-amber-500'
                  )}
                  title={
                    slot.notCollected
                      ? `${slot.date}: your broker trades were not collected, so there is no figure for this day`
                      : `${slot.date}: no trades recorded`
                  }
                  data-testid={slot.notCollected ? 'daily-pnl-uncollected' : 'daily-pnl-gap'}
                />
              )
            }
            const title = `${b.date}: net ${formatCurrency(b.net)}${b.netComplete ? '' : ' (incomplete: some charges unavailable)'}; gross ${formatCurrency(b.gross)}; est. charges ${formatCurrency(b.charges)}`
            return (
              <div
                key={b.date}
                className="flex min-w-[8px] max-w-[40px] flex-1 flex-col"
                title={title}
                data-testid="daily-pnl-bar"
              >
                <div className="flex flex-1 items-end">
                  {b.net > 0 && (
                    <div
                      className="w-full rounded-t bg-green-600/80"
                      style={{ height: `${Math.max(b.pct, 2)}%` }}
                    />
                  )}
                </div>
                <div className="h-px bg-border" />
                <div className="flex flex-1 items-start">
                  {b.net < 0 && (
                    <div
                      className="w-full rounded-b bg-red-600/80"
                      style={{ height: `${Math.max(b.pct, 2)}%` }}
                    />
                  )}
                </div>
              </div>
            )
          })}
        </div>
      </div>
      <div className="mt-1 flex justify-between text-[11px] text-muted-foreground">
        <span>{slots[0].date}</span>
        {slots.length > 1 && <span>{slots[slots.length - 1].date}</span>}
      </div>
      <p className="mt-1 text-xs text-muted-foreground">{DAILY_CHART_CAPTION}</p>
    </div>
  )
}

export function StripTile({
  label,
  children,
  hint,
  testId,
}: {
  label: string
  children: React.ReactNode
  hint?: string
  testId?: string
}) {
  return (
    <div className="rounded-md border p-3 min-w-0" data-testid={testId}>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-1 font-mono text-sm font-semibold break-words">{children}</div>
      {hint && <div className="mt-0.5 text-[11px] text-muted-foreground">{hint}</div>}
    </div>
  )
}

export function Unavailable({ reason }: { reason: string | null | undefined }) {
  return (
    <span className="font-sans font-normal text-muted-foreground" title={reason ?? undefined}>
      — <span className="text-[11px]">{reason ?? 'not available'}</span>
    </span>
  )
}

export function TradeButton({
  trade,
  formatCurrency,
  onSelect,
}: {
  trade: TradeRef
  formatCurrency: Money
  onSelect: (trade: TradeRef) => void
}) {
  // The server picks the largest win / loss on net P&L; a trade without a net shows its gross.
  const amount = trade.net_pnl ?? trade.realized_pnl
  return (
    <button
      type="button"
      className={cn(
        'max-w-full break-words text-left underline-offset-2 hover:underline',
        pnlTextClass(amount)
      )}
      onClick={() => onSelect(trade)}
      aria-label={`Show fills of the ${amount >= 0 ? 'largest win' : 'largest loss'}, ${trade.symbol}`}
      title={
        trade.net_pnl != null
          ? `Net of estimated charges; gross ${formatCurrency(trade.realized_pnl)}`
          : 'Gross: no charge estimate for this trade'
      }
    >
      {formatCurrency(amount)}
      <span className="block break-words font-sans text-[11px] font-normal text-muted-foreground">
        {trade.symbol} {trade.trade_date}
        {trade.net_pnl != null ? ' (net)' : ' (gross)'}
      </span>
    </button>
  )
}

/**
 * One compact row of risk and consistency figures; figures that need more data say why instead
 * of showing a number. Largest win / loss and the maximum drawdown are in the summary only.
 */
export function RiskStrip({
  consistency: c,
  risk,
  formatCurrency,
  compact = false,
}: {
  /** Half-width card: fewer tile columns. */
  compact?: boolean
  consistency: ConsistencyBlock
  /** Net realised drawdown and streaks; when given they replace the gross drawdown and streak tiles. */
  risk?: RiskBlock | null
  formatCurrency: Money
}) {
  return (
    <Card data-testid="risk-strip" className={compact ? 'gap-3 py-4' : undefined}>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">Risk and consistency</CardTitle>
        {risk && (
          <p className="text-xs text-muted-foreground" data-testid="risk-basis">
            Drawdown and streaks: {NET_DRAWDOWN_BASIS}, in rupees, over {risk.positions} completed
            trade{risk.positions === 1 ? '' : 's'}.
            {risk.net_unavailable_positions > 0 &&
              ` ${risk.net_unavailable_positions} trade(s) without a net P&L are left out.`}
          </p>
        )}
      </CardHeader>
      <CardContent>
        <div
          className={
            compact
              ? 'grid grid-cols-2 gap-2 lg:grid-cols-3'
              : 'grid grid-cols-2 gap-2 sm:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-9'
          }
        >
          <StripTile
            label="Avg daily P&L (gross)"
            testId="risk-avg-daily"
            hint={
              c.avg_daily_net != null
                ? `net ${formatCurrency(c.avg_daily_net)}${c.avg_daily_net_complete ? '' : ' (incomplete)'}; ${c.trading_days} trading day${c.trading_days === 1 ? '' : 's'}`
                : undefined
            }
          >
            {c.avg_daily_pnl == null ? (
              <Unavailable reason={c.avg_daily_unavailable_reason} />
            ) : (
              <span className={pnlTextClass(c.avg_daily_pnl)}>
                {formatCurrency(c.avg_daily_pnl)}
              </span>
            )}
          </StripTile>
          {(() => {
            const wd = winningDaysText(c)
            if (wd) {
              return (
                <StripTile label="Winning days (net)" testId="risk-winning-days" hint={wd.hint}>
                  {wd.value}
                </StripTile>
              )
            }
            // An older server: the gross daily counts, labelled as gross.
            return (
              c.trading_days > 1 && (
                <StripTile
                  label="Profitable / losing days (gross)"
                  testId="risk-days"
                  hint={
                    c.flat_days > 0 ? `${c.flat_days} flat` : `${c.trading_days} trading day(s)`
                  }
                >
                  <span className="text-green-700 dark:text-green-600 [.analyzer_&]:text-green-600">
                    {c.profitable_days}
                  </span>
                  {' / '}
                  <span className="text-red-600 dark:text-red-400">{c.losing_days}</span>
                </StripTile>
              )
            )
          })()}
          <StripTile
            label="Max consecutive winning trades"
            testId="risk-win-streak"
            hint={
              risk
                ? `net of estimated charges; break-even ends a streak; ${risk.positions} trade${risk.positions === 1 ? '' : 's'}`
                : c.completed_trades === 0
                  ? 'no completed trades'
                  : undefined
            }
          >
            <span title={c.streak_rule}>
              {risk ? risk.max_consecutive_wins : c.max_consecutive_wins}
            </span>
          </StripTile>
          <StripTile
            label="Max consecutive losing trades"
            testId="risk-loss-streak"
            hint={`break-even ends a streak; ${risk ? risk.positions : c.completed_trades} trade${(risk ? risk.positions : c.completed_trades) === 1 ? '' : 's'}`}
          >
            <span title={c.streak_rule}>
              {risk ? risk.max_consecutive_losses : c.max_consecutive_losses}
            </span>
          </StripTile>
          {risk && (
            <StripTile label="Current streak" testId="risk-current-streak">
              {streakText(risk.current_streak)}
            </StripTile>
          )}
          <StripTile
            label="Avg holding time"
            testId="risk-hold"
            hint={
              c.median_hold_minutes != null
                ? `median ${fmtDuration(c.median_hold_minutes)}; ${c.completed_trades} trade${c.completed_trades === 1 ? '' : 's'}`
                : undefined
            }
          >
            {c.avg_hold_minutes == null ? (
              <Unavailable reason="no completed trades" />
            ) : (
              fmtDuration(c.avg_hold_minutes)
            )}
          </StripTile>
          {risk && (
            <StripTile
              label="Current drawdown"
              testId="risk-current-drawdown"
              hint="below the highest net P&L of the selected period"
            >
              <span
                className={risk.current_drawdown > 0 ? 'text-red-600 dark:text-red-400' : undefined}
              >
                {risk.current_drawdown > 0 ? '-' : ''}
                {formatCurrency(Math.abs(risk.current_drawdown))}
              </span>
            </StripTile>
          )}
        </div>
        <BehaviourTiles consistency={c} risk={risk} />
      </CardContent>
    </Card>
  )
}

/**
 * Objective behaviour figures from the closed trades: drawdown recovery, trades per day, quick
 * re-entry after a loss and position size spread. Descriptions only: no reason is guessed for
 * any of them. Each figure shows its own sample (trading days, losses followed by an entry).
 * A figure the server could not compute (no trade, no loss followed by an entry) is left out
 * instead of shown as a placeholder.
 */
function BehaviourTiles({
  consistency: c,
  risk,
}: {
  consistency: ConsistencyBlock
  risk?: RiskBlock | null
}) {
  if (c.completed_trades < 1) return null
  const recovery = drawdownRecoveryText(risk)
  const perDay = tradesPerDayText(c)
  const reentry = reentryText(c)
  const sizes = quantityText(c)
  const showRecovery = !!risk && recovery.value !== INSUFFICIENT_DATA
  const showPerDay = perDay.value !== INSUFFICIENT_DATA && (c.trades_per_day?.days ?? 0) >= 1
  const showReentry =
    reentry.value !== INSUFFICIENT_DATA &&
    (c.reentry_after_loss?.losses_followed_by_entry ?? 0) >= 1
  if (!(showRecovery || showPerDay || showReentry || sizes.length > 0)) return null
  return (
    <div className="mt-3 space-y-2" data-testid="behaviour-tiles">
      <div className="text-xs font-medium text-muted-foreground">
        Trading behaviour ({c.completed_trades} completed trades)
      </div>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {showRecovery && (
          <StripTile
            label="Drawdown recovery (net)"
            testId="behaviour-drawdown-recovery"
            hint={recovery.hint}
          >
            {recovery.value}
          </StripTile>
        )}
        {showPerDay && (
          <StripTile label="Trades per day" testId="behaviour-trades-per-day" hint={perDay.hint}>
            {perDay.value}
          </StripTile>
        )}
        {showReentry && (
          <StripTile label="Re-entries after a loss" testId="behaviour-reentry" hint={reentry.hint}>
            {reentry.value}
          </StripTile>
        )}
        {sizes.length > 0 && (
          <StripTile
            label="Position size (min / median / max qty)"
            testId="behaviour-quantity"
            hint="quantity only; no capital or planned risk is known"
          >
            <span className="block space-y-0.5">
              {sizes.map((s) => (
                <span key={s.key} className="block">
                  {s.label}: {s.text}
                  <span className="ml-1 font-sans text-[11px] font-normal text-muted-foreground">
                    ({s.positions} trade{s.positions === 1 ? '' : 's'})
                  </span>
                </span>
              ))}
            </span>
          </StripTile>
        )}
      </div>
    </div>
  )
}

function IncompleteMark({ count }: { count: number }) {
  return (
    <span
      className="ml-1 text-xs text-amber-700 dark:text-amber-500 [.analyzer_&]:text-amber-500"
      title={`${count} trade(s) have no charge estimate`}
    >
      incomplete
    </span>
  )
}

/** One table of "How you trade" rows (the server's row builder, the same as By symbol). */
export function HowYouTradeTable({
  rows,
  column,
  formatCurrency,
}: {
  rows: BreakdownRow[]
  column: string
  formatCurrency: Money
}) {
  if (rows.length === 0) {
    return <p className="text-sm text-muted-foreground">None in this period</p>
  }
  return (
    <div className="min-w-0 overflow-x-auto">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>{column}</TableHead>
            <TableHead className="text-right">Trades</TableHead>
            <TableHead className="text-right">Win rate</TableHead>
            <TableHead className="text-right">Avg P&amp;L (net)</TableHead>
            <TableHead className="text-right">Profit factor</TableHead>
            <TableHead className="text-right">Gross P&amp;L</TableHead>
            <TableHead className="text-right">Est. charges</TableHead>
            <TableHead className="text-right">Net of charges</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row) => {
            const pf = rowProfitFactor(row)
            return (
              <TableRow key={row.key}>
                <TableCell className="font-medium">{bucketLabel(row.key ?? '')}</TableCell>
                <TableCell className="text-right font-mono">{row.trades}</TableCell>
                <TableCell className="text-right font-mono">
                  {row.trades === 0 ? '—' : fmtWinRate(row.win_rate)}
                </TableCell>
                <TableCell
                  className={cn(
                    'text-right font-mono',
                    row.trades > 0 && pnlTextClass(row.expectancy ?? 0)
                  )}
                >
                  {row.trades === 0 ? '—' : fmtNetMoney(row.expectancy, formatCurrency)}
                </TableCell>
                <TableCell className="text-right font-mono" title={pf.title}>
                  {pf.value}
                </TableCell>
                <TableCell className={cn('text-right font-mono', pnlTextClass(row.realized_gross))}>
                  {formatCurrency(row.realized_gross)}
                </TableCell>
                <TableCell className="text-right font-mono text-muted-foreground">
                  {formatCurrency(row.charges)}
                </TableCell>
                <TableCell className={cn('text-right font-mono', pnlTextClass(row.net_of_charges))}>
                  {formatCurrency(row.net_of_charges)}
                  {netIncomplete(row) && <IncompleteMark count={row.charges_unavailable_trades} />}
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    </div>
  )
}

/**
 * "How you trade": one table at a time, chosen with a segmented selector. Symbol, side, product
 * and intraday / overnight stay in the Breakdowns card (their existing tables), so this card adds
 * only the entry time of day, entry weekday and holding time.
 */
export function HowYouTradeCard({
  breakdowns,
  formatCurrency,
  keys,
}: {
  breakdowns: TradingBreakdowns
  formatCurrency: Money
  /** Tabs with something to compare; the page decides (see breakdownVisibility). */
  keys?: HowYouTradeKey[]
}) {
  const options = HOW_YOU_TRADE_OPTIONS.filter((o) => !keys || keys.includes(o.key))
  const [picked, setTab] = useState<HowYouTradeKey | null>(null)
  const tab = options.find((o) => o.key === picked)?.key ?? options[0]?.key
  if (!tab) return null
  return (
    <Card data-testid="how-you-trade">
      <CardHeader>
        <CardTitle>How you trade</CardTitle>
      </CardHeader>
      <CardContent>
        <Tabs value={tab} onValueChange={(v) => setTab(v as HowYouTradeKey)}>
          <TabsList aria-label="Group trades by">
            {options.map((o) => (
              <TabsTrigger key={o.key} value={o.key}>
                {o.label}
              </TabsTrigger>
            ))}
          </TabsList>
          {options.map((o) => (
            <TabsContent key={o.key} value={o.key} className="space-y-2">
              {o.key === 'time_of_day' && (
                <NetBarChart
                  title="Net P&L by entry time (IST), trades in brackets"
                  bars={timeOfDayBars(breakdowns.time_of_day)}
                  formatCurrency={formatCurrency}
                  samples="trade(s)"
                  emptyText="No completed trades in this range."
                  testId="time-of-day-chart"
                />
              )}
              <HowYouTradeTable
                rows={breakdowns[o.key] ?? []}
                column={o.column}
                formatCurrency={formatCurrency}
              />
              <p className="text-xs text-muted-foreground">{o.note}</p>
            </TabsContent>
          ))}
        </Tabs>
        <p className="mt-2 text-xs text-muted-foreground">
          Avg P&amp;L is net of estimated charges, per completed trade. The Trades column is the
          number of completed trades behind each row.
        </p>
      </CardContent>
    </Card>
  )
}
