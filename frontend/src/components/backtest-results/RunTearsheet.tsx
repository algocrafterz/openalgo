import type { RunDetail, RunRecord } from '@/api/backtestResults'
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

function HeadlineStats({ run }: { run: RunRecord }) {
  const m = run.metrics
  const net = metricText(m, m.net_bps)
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
      <Stat
        label="Net bps per trade (after costs)"
        value={net}
        hint={`Gross ${metricText(m, m.gross_bps)}. Green only when statistically significant.`}
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
      <Stat
        label="Sharpe"
        value={metricText(m, m.sharpe, (v) => fmtNum(v))}
        hint="Daily R, annualised"
      />
      <Stat
        label="Max drawdown (R)"
        value={metricText(m, m.max_dd_r, (v) => `${fmtNum(v, 1)}R`)}
        hint="Peak to trough of cumulative net R"
      />
      <Stat label="t-statistic vs hurdle" value={tVsHurdle(m)} hint="Hurdle rises with trials" />
    </div>
  )
}

export function RunTearsheet({
  run,
  ideaId,
  assumptions,
}: {
  run: RunRecord
  ideaId?: string
  assumptions?: RunDetail['assumptions']
}) {
  const series = run.series ?? {}
  return (
    <div className="space-y-6" data-testid="run-tearsheet">
      {ideaId && <ExportMenu ideaId={ideaId} runId={run.run_id} hasTrades={run.trades_export} />}
      {isExtrapolated(run.metrics) && (
        <div role="note" className="rounded-md border border-yellow-500/50 p-3 text-sm">
          Only {run.metrics.days} trading days: ratios such as Sharpe are extrapolated from less
          than a year of data and should be read with caution.
        </div>
      )}
      <HeadlineStats run={run} />
      <RealityChecks run={run} />
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
            series={[{ label: 'Drawdown', color: DRAWDOWN_COLOR, points: series.drawdown_r ?? [] }]}
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
      {run.evaluation && <EvaluationTable evaluation={run.evaluation} />}
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
