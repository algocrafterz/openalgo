import type {
  BreakdownRow,
  ConsistencyBlock,
  CostsBlock,
  HoldSummary,
  RiskBlock,
  SampleContext,
  TradeManagementBlock,
  TradeRef,
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
import {
  bucketLabel,
  drawdownRecoveryText,
  fmtNetMoney,
  fmtPct,
  fmtWinRate,
  INSUFFICIENT_DATA,
  netIncomplete,
  pnlTextClass,
  quantityText,
  reentryText,
  rowProfitFactor,
  sampleNote,
  streakText,
  tradesPerDayText,
  winningDaysText,
} from '@/lib/discretionaryPnlView'
import { fmtDuration } from '@/lib/strategyPnlView'
import { cn } from '@/lib/utils'

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

function Duration({
  stat,
}: {
  stat: { count: number; mean_minutes: number | null; median_minutes: number | null }
}) {
  if (stat.count === 0) return <Unavailable reason="none in this range" />
  return <>{fmtDuration(stat.mean_minutes)}</>
}

/**
 * Holding time of winners and losers (each with its median), median net P&L and what charges
 * take from gross profit. Tiles only: they sit in the grid of the Risk and consistency card.
 */
function TradeManagementTiles({
  management,
  costs,
  formatCurrency,
}: {
  management: TradeManagementBlock | null | undefined
  costs: CostsBlock | null | undefined
  formatCurrency: Money
}) {
  if (!management && !costs) return null
  return (
    <div className="contents" data-testid="trade-management">
      {management && (
        <>
          <StripTile
            label="Winners: average hold"
            testId="tm-winner-hold"
            hint={
              management.winners.count > 0
                ? `median ${fmtDuration(management.winners.median_minutes)}, ${management.winners.count} position(s)`
                : undefined
            }
          >
            <Duration stat={management.winners} />
          </StripTile>
          <StripTile
            label="Losers: average hold"
            testId="tm-loser-hold"
            hint={
              management.losers.count > 0
                ? `median ${fmtDuration(management.losers.median_minutes)}, ${management.losers.count} position(s)`
                : undefined
            }
          >
            <Duration stat={management.losers} />
          </StripTile>
          <StripTile
            label="Median net P&L"
            testId="tm-median-net"
            hint={`per position, ${management.net_basis_positions} with a net`}
          >
            {management.median_net_pnl == null ? (
              <Unavailable reason="no position with a net" />
            ) : (
              <span className={pnlTextClass(management.median_net_pnl)}>
                {formatCurrency(management.median_net_pnl)}
              </span>
            )}
          </StripTile>
        </>
      )}
      {costs && (
        <StripTile
          label="Est. charges as % of winners' gross profit"
          testId="cost-pct"
          hint={
            costs.charges_unavailable_positions > 0
              ? `${costs.charges_unavailable_positions} position(s) without a charge estimate not included`
              : undefined
          }
        >
          {costs.charges_pct_of_gross_profit == null ? (
            <Unavailable reason={costs.reason ?? 'not available'} />
          ) : (
            fmtPct(costs.charges_pct_of_gross_profit)
          )}
        </StripTile>
      )}
    </div>
  )
}

/**
 * Risk and consistency, with trade management and costs: one card of tiles. Average daily P&L
 * leads with net like the rest of the page. The maximum and current drawdown are in the
 * summary's key figures only, so a drawdown is stated once. Figures that need more data say why
 * instead of showing a number.
 */
export function RiskStrip({
  consistency: c,
  risk,
  management,
  costs,
  hold,
  formatCurrency,
}: {
  /** Absent on an older server: only the trade-management and hold tiles are then shown. */
  consistency?: ConsistencyBlock | null
  /** Net realised streaks and trade counts; when given they replace the gross streak tiles. */
  risk?: RiskBlock | null
  management?: TradeManagementBlock | null
  costs?: CostsBlock | null
  /** Older servers without the trade-management block: the hold figures they have. */
  hold?: HoldSummary | null
  formatCurrency: Money
}) {
  const days = c ? `${c.trading_days} trading day${c.trading_days === 1 ? '' : 's'}` : ''
  return (
    <Card data-testid="risk-strip" className="gap-3 py-4">
      <CardHeader className="pb-2">
        <CardTitle className="text-base">Risk and consistency</CardTitle>
        {risk && (
          <p className="text-xs text-muted-foreground" data-testid="risk-basis">
            Streaks: net after estimated charges, over {risk.positions} completed trade
            {risk.positions === 1 ? '' : 's'}.
            {risk.net_unavailable_positions > 0 &&
              ` ${risk.net_unavailable_positions} trade(s) without a net P&L are left out.`}
          </p>
        )}
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-4">
          {c && (
            <>
              {c.avg_daily_net != null ? (
                <StripTile
                  label="Avg daily P&L (net)"
                  testId="risk-avg-daily"
                  hint={`${c.avg_daily_net_complete ? '' : 'incomplete: some charges unavailable; '}${
                    c.avg_daily_pnl != null ? `gross ${formatCurrency(c.avg_daily_pnl)}; ` : ''
                  }${days}`}
                >
                  <span className={pnlTextClass(c.avg_daily_net)}>
                    {formatCurrency(c.avg_daily_net)}
                  </span>
                </StripTile>
              ) : (
                <StripTile label="Avg daily P&L (gross)" testId="risk-avg-daily">
                  {c.avg_daily_pnl == null ? (
                    <Unavailable reason={c.avg_daily_unavailable_reason} />
                  ) : (
                    <span className={pnlTextClass(c.avg_daily_pnl)}>
                      {formatCurrency(c.avg_daily_pnl)}
                    </span>
                  )}
                </StripTile>
              )}
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
                    ? `net of estimated charges; ${risk.positions} trade${risk.positions === 1 ? '' : 's'}`
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
            </>
          )}
          <TradeManagementTiles
            management={management}
            costs={costs}
            formatCurrency={formatCurrency}
          />
          {!management && hold && (
            // an older server without the trade-management block: the hold figures it has
            <div className="contents" data-testid="hold-durations">
              {(
                [
                  ...(c ? [] : ([['Overall: average hold', hold.overall]] as const)),
                  ['Winners: average hold', hold.winners],
                  ['Losers: average hold', hold.losers],
                ] as const
              ).map(([label, stat]) => (
                <StripTile
                  key={label}
                  label={label}
                  hint={
                    stat && stat.count > 0
                      ? `median ${fmtDuration(stat.median_minutes)}, ${stat.count} trade(s)`
                      : undefined
                  }
                >
                  {fmtDuration(stat?.mean_minutes)}
                </StripTile>
              ))}
            </div>
          )}
        </div>
        {management &&
          (management.break_even_excluded > 0 || management.net_unavailable_positions > 0) && (
            <p className="mt-1 text-xs text-muted-foreground">
              {management.break_even_excluded > 0 &&
                `${management.break_even_excluded} break-even position(s) are in neither winners nor losers. `}
              {management.net_unavailable_positions > 0 &&
                `${management.net_unavailable_positions} position(s) without a net P&L are left out.`}
            </p>
          )}
        {c && <BehaviourTiles consistency={c} risk={risk} />}
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
