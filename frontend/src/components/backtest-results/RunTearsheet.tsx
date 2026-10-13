import type {
  GateCorrection,
  MonthlyRow,
  PortfolioView,
  RunDetail,
  RunRecord,
  TradeSubset,
} from '@/api/backtestResults'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  fmtNum,
  fmtPercent,
  isExtrapolated,
  isKnown,
  metricText,
  netClass,
  tVsHurdle,
} from '@/lib/backtestResults'
import { cn } from '@/lib/utils'
import { EvaluationTable } from './EvaluationTable'
import { ExportMenu } from './ExportMenu'
import { LineChart } from './LineChart'
import { MonthlyGrid } from './MonthlyGrid'
import { RealityChecks } from './RealityChecks'
import { RegimeTable } from './RegimeTable'
import { RunAssumptions } from './RunAssumptions'
import { RunRobustness } from './RunRobustness'
import { PortfolioChart } from './RunSummary'

const EQUITY_COLOR = '#3b82f6'
const DRAWDOWN_COLOR = '#eab308'

function Stat({
  label,
  value,
  hint,
  valueClass,
}: {
  label: string
  value: string
  hint?: string
  valueClass?: string
}) {
  return (
    <div className="rounded-md border p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={cn('text-xl font-semibold font-mono', valueClass)}>{value}</div>
      {hint && <div className="text-xs text-muted-foreground mt-1">{hint}</div>}
    </div>
  )
}

function HeadlineStats({ run, portfolio }: { run: RunRecord; portfolio?: PortfolioView | null }) {
  const m = run.metrics
  const net = metricText(m, m.net_bps)
  const book = portfolio && !portfolio.unavailable ? portfolio.stats : undefined
  // A run whose trades have no stop records no R expectancy: its results are in bps.
  const noR = m.expectancy_r === undefined
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
      <Stat
        label="Mean net bps per trade (after costs)"
        value={net}
        hint={`Gross ${metricText(m, m.gross_bps)}. Average over ${m.trades} trades, in bps of each position's notional (100 bps = 1%); not a portfolio return. Green only when statistically significant.`}
        valueClass={netClass(m)}
      />
      <Stat label="Trades" value={m.trades === undefined ? 'n/a' : String(m.trades)} />
      <Stat label="Win rate" value={isKnown(m, m.win_rate) ? fmtPercent(m.win_rate) : 'n/a'} />
      <Stat
        label="Profit factor"
        value={metricText(m, m.profit_factor, (v) => fmtNum(v))}
        hint="Gross wins divided by gross losses, after costs"
      />
      <Stat label="Expectancy (R)" value={metricText(m, m.expectancy_r)} />
      {book?.sharpe !== undefined && book.sharpe !== null ? (
        <Stat
          label="Sharpe (portfolio)"
          value={fmtNum(book.sharpe)}
          hint={`Daily book returns over all ${book.sessions} sessions, annualised. The record's ${metricText(m, m.sharpe, (v) => fmtNum(v))} sums trades by entry session and is not a portfolio Sharpe.`}
        />
      ) : (
        <Stat
          label="Sharpe"
          value={metricText(m, m.sharpe, (v) => fmtNum(v))}
          hint={
            noR
              ? `Trade results summed by entry session (${m.days} sessions) and annualised as if daily: not a portfolio Sharpe`
              : 'Daily R, annualised'
          }
        />
      )}
      {book?.max_drawdown_pct !== undefined ? (
        <Stat
          label="Max drawdown (portfolio)"
          value={`${fmtNum(book.max_drawdown_pct, 1)}%`}
          hint="Peak to trough of the daily book, % of its prior peak"
        />
      ) : (
        <Stat
          label="Max drawdown (R)"
          value={metricText(m, m.max_dd_r, (v) => `${fmtNum(v, 1)}R`)}
          hint="Peak to trough of cumulative net R"
        />
      )}
      <Stat label="t-statistic vs hurdle" value={tVsHurdle(m)} hint="Hurdle rises with trials" />
    </div>
  )
}

function bookMonths(portfolio?: PortfolioView | null): MonthlyRow[] {
  return (portfolio?.monthly_pct ?? []).map((r) => ({
    month: r.month,
    n: 0,
    total_r: r.return_pct,
    net_r: r.return_pct,
  }))
}

export function RunTearsheet({
  run,
  ideaId,
  assumptions,
  onShowTrades,
  portfolio,
  corrections,
}: {
  run: RunRecord
  ideaId?: string
  assumptions?: RunDetail['assumptions']
  onShowTrades?: (subset: TradeSubset) => void
  /** The daily book (recorded or rebuilt), when the run has one. */
  portfolio?: PortfolioView | null
  /** Gates re-scored after an evaluator fix; the table shows both readings. */
  corrections?: GateCorrection[]
}) {
  const series = run.series ?? {}
  const book =
    portfolio && !portfolio.unavailable && portfolio.equity_pct?.length ? portfolio : null
  const months = bookMonths(book)
  return (
    <div className="space-y-6" data-testid="run-tearsheet">
      {ideaId && <ExportMenu ideaId={ideaId} runId={run.run_id} hasTrades={run.trades_export} />}
      {isExtrapolated(run.metrics, run.years) && (
        <div role="note" className="rounded-md border border-yellow-500/50 p-3 text-sm">
          {typeof run.years === 'number'
            ? `Only ${run.years} years tested`
            : `Trades entered on only ${run.metrics.days} sessions`}
          : ratios such as Sharpe are extrapolated from less than a year of data and should be read
          with caution.
        </div>
      )}
      <HeadlineStats run={run} portfolio={portfolio} />
      <RealityChecks run={run} />
      {book ? (
        <>
          <PortfolioChart portfolio={book} />
          <Card>
            <CardHeader>
              <CardTitle>Monthly book return (%)</CardTitle>
            </CardHeader>
            <CardContent>
              <MonthlyGrid
                rows={months}
                caption="Book return per month, % of the book"
                cellTitle={(r) => `${fmtNum(r.total_r)}% of the book`}
              />
            </CardContent>
          </Card>
        </>
      ) : (
        <>
          <Card>
            <CardHeader>
              <CardTitle>Equity and drawdown</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-6 lg:grid-cols-2">
              <LineChart
                title="Cumulative net R"
                series={[{ label: 'Net R', color: EQUITY_COLOR, points: series.equity_r ?? [] }]}
              />
              <LineChart
                title="Drawdown (R below prior peak)"
                series={[
                  { label: 'Drawdown', color: DRAWDOWN_COLOR, points: series.drawdown_r ?? [] },
                ]}
              />
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Monthly net R</CardTitle>
            </CardHeader>
            <CardContent>
              <MonthlyGrid rows={series.monthly ?? []} />
            </CardContent>
          </Card>
        </>
      )}
      {run.evaluation && (
        <EvaluationTable
          evaluation={run.evaluation}
          onShowTrades={onShowTrades}
          corrections={corrections}
        />
      )}
      {run.regimes && <RegimeTable regimes={run.regimes} />}
      <RunRobustness run={run} />
      <RunAssumptions run={run} assumptions={assumptions} />
      <p className="text-xs text-muted-foreground" data-testid="caveat">
        Backtests overstate live performance: fills, slippage, data quality and regime change all
        cut results after a strategy goes live. No metric on this page is a threshold that proves a
        strategy works. Treat a result as evidence to test forward, not a forecast.
      </p>
    </div>
  )
}
