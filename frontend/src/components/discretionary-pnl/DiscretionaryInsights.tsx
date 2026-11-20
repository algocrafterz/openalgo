import { Info } from 'lucide-react'
import { useState } from 'react'
import type {
  BreakdownRow,
  ConsistencyBlock,
  DataQuality,
  PeriodRow,
  SampleContext,
  TradeRef,
  TradingBreakdowns,
} from '@/api/discretionaryPnl'
import { Badge } from '@/components/ui/badge'
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
import {
  attributionCards,
  bucketLabel,
  DAILY_CHART_CAPTION,
  dailyBars,
  dataQualityPrices,
  dataQualityStates,
  fmtStamp,
  fmtWinRate,
  HOW_YOU_TRADE_OPTIONS,
  type HowYouTradeKey,
  netIncomplete,
  rowProfitFactor,
  sampleNote,
  syncSummary,
} from '@/lib/discretionaryPnlView'
import { fmtDuration, pnlClass } from '@/lib/strategyPnlView'
import { cn } from '@/lib/utils'

type Money = (value: number) => string

/** "Based on N completed trades" under the metric cards, with the low-sample note. */
export function SampleNote({ sample }: { sample: SampleContext | undefined }) {
  const note = sampleNote(sample)
  if (!note) return null
  return (
    <p
      className={cn('text-xs', note.low ? 'text-amber-600' : 'text-muted-foreground')}
      data-testid="discretionary-sample-note"
    >
      {note.text}
    </p>
  )
}

/**
 * Daily realized P&L bars (gross, the equity curve's basis) from the server's daily rows, with
 * the profitable / losing day count. Plain bars: every bar has a tooltip with charges and net.
 */
export function DailyPnlChart({
  rows,
  formatCurrency,
}: {
  rows: PeriodRow[]
  formatCurrency: Money
}) {
  const { bars, profitable, losing, flat } = dailyBars(rows)
  if (bars.length === 0) return null
  return (
    <div data-testid="daily-pnl-chart">
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <div className="text-sm font-medium">Daily P&amp;L (gross)</div>
        <div className="text-xs text-muted-foreground" data-testid="daily-pnl-days">
          <span className="text-green-600">{profitable} profitable</span>
          {' / '}
          <span className="text-red-600">{losing} losing</span>
          {flat > 0 ? ` / ${flat} flat` : ''} day(s)
        </div>
      </div>
      <div className="overflow-x-auto">
        <div
          className="flex h-40 items-stretch gap-1 border-y border-dashed border-border px-1"
          style={{ minWidth: `${bars.length * 12}px` }}
          role="img"
          aria-label={`Daily gross P&L over ${bars.length} trading day(s): ${profitable} profitable, ${losing} losing`}
        >
          {bars.map((b) => {
            const title = `${b.date}: gross ${formatCurrency(b.gross)}; est. charges ${formatCurrency(b.charges)}; net ${formatCurrency(b.net)}${b.netComplete ? '' : ' (incomplete: some charges unavailable)'}`
            return (
              <div
                key={b.date}
                className="flex min-w-[8px] max-w-[40px] flex-1 flex-col"
                title={title}
                data-testid="daily-pnl-bar"
              >
                <div className="flex flex-1 items-end">
                  {b.gross > 0 && (
                    <div
                      className="w-full rounded-t bg-green-600/80"
                      style={{ height: `${Math.max(b.pct, 2)}%` }}
                    />
                  )}
                </div>
                <div className="h-px bg-border" />
                <div className="flex flex-1 items-start">
                  {b.gross < 0 && (
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
        <span>{bars[0].date}</span>
        {bars.length > 1 && <span>{bars[bars.length - 1].date}</span>}
      </div>
      <p className="mt-1 text-xs text-muted-foreground">{DAILY_CHART_CAPTION}</p>
    </div>
  )
}

function StripTile({
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

function Unavailable({ reason }: { reason: string | null | undefined }) {
  return (
    <span className="font-sans font-normal text-muted-foreground" title={reason ?? undefined}>
      — <span className="text-[11px]">{reason ?? 'not available'}</span>
    </span>
  )
}

function TradeButton({
  trade,
  formatCurrency,
  onSelect,
}: {
  trade: TradeRef
  formatCurrency: Money
  onSelect: (trade: TradeRef) => void
}) {
  return (
    <button
      type="button"
      className={cn('text-left underline-offset-2 hover:underline', pnlClass(trade.realized_pnl))}
      onClick={() => onSelect(trade)}
      aria-label={`Show fills of the ${trade.realized_pnl >= 0 ? 'largest win' : 'largest loss'}, ${trade.symbol}`}
    >
      {formatCurrency(trade.realized_pnl)}
      <span className="ml-1 font-sans text-[11px] font-normal text-muted-foreground">
        {trade.symbol} {trade.trade_date}
      </span>
    </button>
  )
}

/**
 * One compact row of risk and consistency figures. Largest win / loss open the fills
 * drill-down; figures that need more data say why instead of showing a number.
 */
export function RiskStrip({
  consistency: c,
  formatCurrency,
  onSelectTrade,
}: {
  consistency: ConsistencyBlock
  formatCurrency: Money
  onSelectTrade: (trade: TradeRef) => void
}) {
  return (
    <Card data-testid="risk-strip">
      <CardHeader className="pb-2">
        <CardTitle className="text-base">Risk and consistency</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 xl:grid-cols-8">
          <StripTile label="Largest win" testId="risk-largest-win">
            {c.largest_win ? (
              <TradeButton
                trade={c.largest_win}
                formatCurrency={formatCurrency}
                onSelect={onSelectTrade}
              />
            ) : (
              <Unavailable reason="no winning trade" />
            )}
          </StripTile>
          <StripTile label="Largest loss" testId="risk-largest-loss">
            {c.largest_loss ? (
              <TradeButton
                trade={c.largest_loss}
                formatCurrency={formatCurrency}
                onSelect={onSelectTrade}
              />
            ) : (
              <Unavailable reason="no losing trade" />
            )}
          </StripTile>
          <StripTile
            label="Avg daily P&L (gross)"
            testId="risk-avg-daily"
            hint={
              c.avg_daily_net != null
                ? `net ${formatCurrency(c.avg_daily_net)}${c.avg_daily_net_complete ? '' : ' (incomplete)'}`
                : undefined
            }
          >
            {c.avg_daily_pnl == null ? (
              <Unavailable reason={c.avg_daily_unavailable_reason} />
            ) : (
              <span className={pnlClass(c.avg_daily_pnl)}>{formatCurrency(c.avg_daily_pnl)}</span>
            )}
          </StripTile>
          <StripTile
            label="Profitable / losing days"
            testId="risk-days"
            hint={c.flat_days > 0 ? `${c.flat_days} flat` : `${c.trading_days} trading day(s)`}
          >
            <span className="text-green-600">{c.profitable_days}</span>
            {' / '}
            <span className="text-red-600">{c.losing_days}</span>
          </StripTile>
          <StripTile
            label="Max consecutive winning trades"
            testId="risk-win-streak"
            hint={c.completed_trades === 0 ? 'no completed trades' : undefined}
          >
            <span title={c.streak_rule}>{c.max_consecutive_wins}</span>
          </StripTile>
          <StripTile
            label="Max consecutive losing trades"
            testId="risk-loss-streak"
            hint="break-even ends a streak"
          >
            <span title={c.streak_rule}>{c.max_consecutive_losses}</span>
          </StripTile>
          <StripTile
            label="Avg holding time"
            testId="risk-hold"
            hint={
              c.median_hold_minutes != null
                ? `median ${fmtDuration(c.median_hold_minutes)}`
                : undefined
            }
          >
            {c.avg_hold_minutes == null ? (
              <Unavailable reason="no completed trades" />
            ) : (
              fmtDuration(c.avg_hold_minutes)
            )}
          </StripTile>
          <StripTile
            label="Max drawdown"
            testId="risk-drawdown"
            hint="gross realized; excludes charges and unrealized"
          >
            {c.max_drawdown == null ? (
              <Unavailable reason={c.max_drawdown_unavailable_reason} />
            ) : (
              <span className={c.max_drawdown < 0 ? 'text-red-600' : undefined}>
                {formatCurrency(Math.abs(c.max_drawdown))}
              </span>
            )}
          </StripTile>
        </div>
      </CardContent>
    </Card>
  )
}

/** Muted marker beside a ratio that rests on a sample below the edge-check rule. */
export function LowSampleMark() {
  return (
    <span
      className="ml-1 font-sans text-[11px] font-normal text-muted-foreground"
      data-testid="low-sample-mark"
    >
      low sample
    </span>
  )
}

function IncompleteMark({ count }: { count: number }) {
  return (
    <span
      className="ml-1 text-xs text-amber-600"
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
  sample,
  formatCurrency,
}: {
  rows: BreakdownRow[]
  column: string
  sample?: SampleContext
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
            <TableHead className="text-right">Avg P&amp;L (gross)</TableHead>
            <TableHead className="text-right">Profit factor</TableHead>
            <TableHead className="text-right">Gross P&amp;L</TableHead>
            <TableHead className="text-right">Est. charges</TableHead>
            <TableHead className="text-right">Net of charges</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row) => {
            const pf = rowProfitFactor(row, sample)
            return (
              <TableRow key={row.key}>
                <TableCell className="font-medium">{bucketLabel(row.key ?? '')}</TableCell>
                <TableCell className="text-right font-mono">{row.trades}</TableCell>
                <TableCell className="text-right font-mono">
                  {row.trades === 0 ? '—' : fmtWinRate(row.win_rate)}
                </TableCell>
                <TableCell
                  className={cn('text-right font-mono', row.trades > 0 && pnlClass(row.expectancy))}
                >
                  {row.trades === 0 ? '—' : formatCurrency(row.expectancy)}
                </TableCell>
                <TableCell className="text-right font-mono" title={pf.title}>
                  {pf.value}
                  {pf.low && <LowSampleMark />}
                </TableCell>
                <TableCell className={cn('text-right font-mono', pnlClass(row.realized_gross))}>
                  {formatCurrency(row.realized_gross)}
                </TableCell>
                <TableCell className="text-right font-mono text-muted-foreground">
                  {formatCurrency(row.charges)}
                </TableCell>
                <TableCell className={cn('text-right font-mono', pnlClass(row.net_of_charges))}>
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
  sample,
  formatCurrency,
}: {
  breakdowns: TradingBreakdowns
  sample?: SampleContext
  formatCurrency: Money
}) {
  const [tab, setTab] = useState<HowYouTradeKey>('time_of_day')
  return (
    <Card data-testid="how-you-trade">
      <CardHeader>
        <CardTitle>How you trade</CardTitle>
      </CardHeader>
      <CardContent>
        <Tabs value={tab} onValueChange={(v) => setTab(v as HowYouTradeKey)}>
          <TabsList aria-label="Group trades by">
            {HOW_YOU_TRADE_OPTIONS.map((o) => (
              <TabsTrigger key={o.key} value={o.key}>
                {o.label}
              </TabsTrigger>
            ))}
          </TabsList>
          {HOW_YOU_TRADE_OPTIONS.map((o) => (
            <TabsContent key={o.key} value={o.key} className="space-y-2">
              <HowYouTradeTable
                rows={breakdowns[o.key] ?? []}
                column={o.column}
                sample={sample}
                formatCurrency={formatCurrency}
              />
              <p className="text-xs text-muted-foreground">{o.note}</p>
            </TabsContent>
          ))}
        </Tabs>
        <p className="mt-2 text-xs text-muted-foreground">
          Avg P&amp;L is gross per completed trade. A group with fewer than{' '}
          {sample?.min_trades ?? 30} completed trades over {sample?.min_days ?? 10} trading days
          (the edge-check sample rule) is marked low sample: its ratios are indicative only.
        </p>
      </CardContent>
    </Card>
  )
}

/**
 * Data quality of the selection: where every collected execution is (one state each), charges
 * without an estimate, price coverage and the ledger sync.
 */
export function DataQualityStrip({
  dq,
  sourceLabels,
}: {
  dq: DataQuality
  /** `source_labels` of the summary (same period and filters); absent from older servers. */
  sourceLabels?: Record<string, number> | null
}) {
  const states = dataQualityStates(dq)
  const attribution = attributionCards(dq, sourceLabels)
  const sync = syncSummary({
    last_successful_sync: dq.sync.last_successful_sync,
    days_without_sync: dq.sync.days_without_sync,
    runs_by_day: {},
  })
  const problems = dq.sync.problems ?? []
  return (
    <div className="rounded-md border p-3 text-sm" data-testid="data-quality">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Info className="h-4 w-4 text-muted-foreground" />
        <span className="font-medium">Data quality</span>
        <span className="text-xs text-muted-foreground">executions of this period and filters</span>
        {!dq.reconciles && (
          <Badge variant="outline" className="border-red-600/40 text-red-600">
            counts do not add up
          </Badge>
        )}
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-5">
        {states.map((s) => (
          <div
            key={s.key}
            className="rounded border px-2 py-1.5"
            data-testid={`dq-${s.key}`}
            title={s.title}
          >
            <div className="text-[11px] text-muted-foreground">
              {s.step}. {s.label}
            </div>
            <div className="font-mono font-semibold">{s.count}</div>
            {s.detail && <div className="text-[11px] text-muted-foreground">{s.detail}</div>}
          </div>
        ))}
      </div>
      <div className="mt-3 text-[11px] font-medium text-muted-foreground">Attribution</div>
      <div className="mt-1 grid grid-cols-2 gap-2 sm:grid-cols-3" data-testid="dq-attribution">
        {attribution.map((c) => (
          <div key={c.key} className="rounded border px-2 py-1.5" data-testid={`dq-attr-${c.key}`}>
            <div className="text-[11px] text-muted-foreground">{c.label}</div>
            <div className="font-mono font-semibold">{c.count}</div>
            <div className="text-[11px] text-muted-foreground">{c.detail}</div>
            {c.note && (
              <div className="text-[10px] text-muted-foreground" data-testid="dq-attr-window">
                {c.note}
              </div>
            )}
          </div>
        ))}
      </div>
      <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <li data-testid="dq-charges">
          {dq.charges_incomplete_trades > 0 || dq.charges_incomplete_legs > 0 ? (
            <span className="text-amber-600">
              Charges: {dq.charges_incomplete_trades} trade(s) / {dq.charges_incomplete_legs}{' '}
              exit(s) without an estimate
            </span>
          ) : (
            'Charges: every trade has an estimate'
          )}
        </li>
        <li data-testid="dq-prices">{dataQualityPrices(dq)}</li>
        <li data-testid="dq-sync" className={cn(sync.problem && 'text-amber-600')}>
          {sync.text}
        </li>
        {dq.earliest_attributable_at && dq.awaiting_grace > 0 && (
          <li>Earliest attribution {fmtStamp(dq.earliest_attributable_at)} IST</li>
        )}
      </ul>
      {problems.length > 0 && (
        // the lines themselves are in the warnings banner above; not repeated here
        <p className="mt-1 text-xs text-amber-600" data-testid="dq-sync-problems">
          {problems.length} sync problem(s): see the warnings above.
        </p>
      )}
    </div>
  )
}
