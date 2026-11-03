import { useQuery } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import type {
  BucketRow,
  DateRange,
  ExcursionBlock,
  Grade,
  PerformanceInsights,
  PerformancePeriod,
  RollingPoint,
  StrategySelection,
} from '@/api/strategyPnl'
import { strategyPnlApi } from '@/api/strategyPnl'
import { MetricCard } from '@/components/strategy-pnl/MetricCard'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { fmtR, gradeLabel, pnlClass } from '@/lib/strategyPnlView'
import { cn } from '@/lib/utils'

type Money = (value: number) => string

const dash = '—'
const pct = (v: number | null | undefined, digits = 1) =>
  v === null || v === undefined ? dash : `${v.toFixed(digits)}%`
const num = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined ? dash : v.toFixed(digits)

const GRADE_CLASS: Record<Grade, string> = {
  poor: 'bg-red-600/15 text-red-600',
  acceptable: 'bg-amber-500/15 text-amber-600',
  good: 'bg-green-600/15 text-green-600',
  excellent: 'bg-emerald-600/20 text-emerald-600',
}

export function GradeBadge({
  grade,
  preliminary,
}: {
  grade: Grade | null | undefined
  preliminary?: Grade | null
}) {
  if (!grade && preliminary) {
    return (
      <span
        className="rounded border border-dashed px-1.5 py-0.5 text-xs text-muted-foreground"
        title="Preliminary: return measures only. Risk measures need 60 sessions. Not evidence of a proven edge."
      >
        Preliminary: {gradeLabel(preliminary)}
      </span>
    )
  }
  if (!grade) return <span className="text-muted-foreground">{dash}</span>
  return (
    <span className={cn('rounded px-1.5 py-0.5 text-xs font-medium', GRADE_CLASS[grade])}>
      {gradeLabel(grade)}
    </span>
  )
}

/** A bare line chart: no axes, just the shape, with the latest value beside it. */
function Sparkline({ points, label }: { points: (number | null)[]; label: string }) {
  const values = points.filter((v): v is number => v !== null)
  if (values.length < 2)
    return <div className="text-xs text-muted-foreground">not enough trades yet</div>
  const lo = Math.min(...values, 0)
  const hi = Math.max(...values, 0)
  const span = hi - lo || 1
  const w = 220
  const h = 44
  const step = w / (points.length - 1)
  const y = (v: number) => h - ((v - lo) / span) * h
  const path = points
    .map((v, i) => (v === null ? null : `${i * step},${y(v).toFixed(1)}`))
    .filter(Boolean)
    .join(' ')
  return (
    <svg width={w} height={h} role="img" aria-label={label} className="overflow-visible">
      <title>{label}</title>
      <line
        x1={0}
        x2={w}
        y1={y(0)}
        y2={y(0)}
        className="stroke-muted-foreground/40"
        strokeDasharray="3 3"
      />
      <polyline points={path} fill="none" className="stroke-primary" strokeWidth={1.5} />
    </svg>
  )
}

function Rolling({
  title,
  points,
  money,
  window,
  field,
}: {
  title: string
  points: RollingPoint[]
  money: Money
  window: number
  field: 'exp20' | 'pf50'
}) {
  const series = points.map((p) => p[field])
  const last = [...series].reverse().find((v) => v !== null) ?? null
  const text = field === 'exp20' ? (last === null ? dash : money(last)) : num(last)
  return (
    <div className="space-y-1">
      <div className="text-xs text-muted-foreground">
        {title} ({window} trades)
      </div>
      <div className="flex items-center gap-3">
        <Sparkline points={series} label={`${title} rolling ${window} trades`} />
        <span className="font-mono text-sm">{text}</span>
      </div>
    </div>
  )
}

function BucketTable({ title, rows, money }: { title: string; rows: BucketRow[]; money: Money }) {
  if (rows.length === 0) return null
  return (
    <div>
      <div className="text-sm font-medium mb-2">{title}</div>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead />
            <TableHead className="text-right">Trades</TableHead>
            <TableHead className="text-right">Win rate</TableHead>
            <TableHead className="text-right">Net P&amp;L</TableHead>
            <TableHead className="text-right">Net PF</TableHead>
            <TableHead className="text-right">Net/trade</TableHead>
            <TableHead className="text-right">Avg R</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((r) => (
            <TableRow key={String(r.label)}>
              <TableCell>{typeof r.label === 'number' ? `${r.label}:00` : r.label}</TableCell>
              <TableCell className="text-right font-mono">{r.trades}</TableCell>
              <TableCell className="text-right font-mono">{pct(r.win_rate)}</TableCell>
              <TableCell className={cn('text-right font-mono', pnlClass(r.net))}>
                {money(r.net)}
              </TableCell>
              <TableCell className="text-right font-mono">{num(r.profit_factor)}</TableCell>
              <TableCell className={cn('text-right font-mono', pnlClass(r.expectancy))}>
                {money(r.expectancy)}
              </TableCell>
              <TableCell className="text-right font-mono">
                {r.expectancy_r === null ? dash : fmtR(r.expectancy_r)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

const METRIC_NAMES: Record<string, string> = {
  net_profit_factor: 'Net profit factor',
  win_rate_pct: 'Win rate',
  expectancy_r: 'Expectancy (R, after charges)',
  sharpe: 'Sharpe',
  sortino: 'Sortino',
  recovery_factor: 'Recovery factor',
  cost_pct_of_gross: 'Charges as share of gross profit',
}

function GradingCard({ insights }: { insights: PerformanceInsights }) {
  const g = insights.grading
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-3">
          Grading <GradeBadge grade={g.overall} preliminary={g.preliminary} />
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-xs text-muted-foreground">
          {g.overall
            ? 'The overall grade is the weakest of the graded measures, so a few strong numbers cannot lift it. Win rate is shown but never sets it.'
            : g.preliminary
              ? 'Preliminary: this grade uses return measures only. The risk measures need more history, so it is not a validated assessment and not evidence of a proven edge. '
              : (g.overall_note ?? 'Not graded.')}{' '}
          Bands are screening guides, not a promise about future results. Based on {g.sample.trades}{' '}
          trades over {g.sample.sessions} trading sessions.
        </p>
        <Table>
          <TableBody>
            {Object.entries(g.metrics)
              .sort(
                ([a], [b]) =>
                  Object.keys(METRIC_NAMES).indexOf(a) - Object.keys(METRIC_NAMES).indexOf(b)
              )
              .map(([name, m]) => (
                <TableRow key={name}>
                  <TableCell>{METRIC_NAMES[name] ?? name}</TableCell>
                  <TableCell className="text-right font-mono">{num(m.metric_value)}</TableCell>
                  <TableCell className="text-right">
                    {m.grade ? (
                      <GradeBadge grade={m.grade} />
                    ) : (
                      <span className="text-xs text-muted-foreground">{m.reason}</span>
                    )}
                  </TableCell>
                </TableRow>
              ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  )
}

export function InsightsPanel({
  insights,
  formatCurrency: money,
}: {
  insights: PerformanceInsights
  formatCurrency: Money
}) {
  const c = insights.concentration
  const d = insights.drawdown
  const names = Object.keys(insights.rolling.by_strategy)
  const eq = insights.entry_quality
  const sl = insights.slippage
  return (
    <div className="space-y-6">
      <GradingCard insights={insights} />

      <Card>
        <CardHeader>
          <CardTitle>Consistency</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid grid-cols-1 min-[480px]:grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
            <MetricCard
              label="Profitable days"
              value={pct(c.profitable_days_pct)}
              hint={`${c.profitable_days ?? 0} of ${c.traded_days} traded days`}
            />
            <MetricCard
              label="Average winning day"
              value={c.avg_winning_day == null ? dash : money(c.avg_winning_day)}
            />
            <MetricCard
              label="Average losing day"
              value={c.avg_losing_day == null ? dash : money(c.avg_losing_day)}
            />
            <MetricCard
              label="Median day"
              value={c.median_daily == null ? dash : money(c.median_daily)}
              hint={c.mean_daily == null ? undefined : `average ${money(c.mean_daily)}`}
            />
            <MetricCard label="Best day" value={c.best_day == null ? dash : money(c.best_day)} />
            <MetricCard label="Worst day" value={c.worst_day == null ? dash : money(c.worst_day)} />
          </div>
          <div className="grid grid-cols-1 min-[480px]:grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
            <MetricCard label="Best day share of profit" value={pct(c.top1_days_share_pct)} />
            <MetricCard label="Best 3 days share" value={pct(c.top3_days_share_pct)} />
            <MetricCard label="Best 5 days share" value={pct(c.top5_days_share_pct)} />
            <MetricCard
              label="Profit without best day"
              value={c.net_ex_best_day == null ? dash : money(c.net_ex_best_day)}
              positive={(c.net_ex_best_day ?? 0) > 0}
              negative={(c.net_ex_best_day ?? 0) < 0}
            />
            <MetricCard
              label="Profit without best 5 days"
              value={c.net_ex_best_five_days == null ? dash : money(c.net_ex_best_five_days)}
              positive={(c.net_ex_best_five_days ?? 0) > 0}
              negative={(c.net_ex_best_five_days ?? 0) < 0}
            />
            <MetricCard
              label={`Best ${c.top10pct_trades ?? 0} trades (10%)`}
              value={pct(c.top10pct_trades_share_of_net_pct)}
              hint="share of net profit"
            />
          </div>
          {c.by_strategy && Object.keys(c.by_strategy).length > 1 && (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Strategy</TableHead>
                  <TableHead className="text-right">Net P&amp;L</TableHead>
                  <TableHead className="text-right">Share of profit</TableHead>
                  <TableHead className="text-right">Best day share of own</TableHead>
                  <TableHead className="text-right">Without its best day</TableHead>
                  <TableHead className="text-right">Without the portfolio's best day</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {Object.entries(c.by_strategy).map(([name, s]) => (
                  <TableRow key={name}>
                    <TableCell>{name}</TableCell>
                    <TableCell className={cn('text-right font-mono', pnlClass(s.net))}>
                      {money(s.net)}
                    </TableCell>
                    <TableCell className="text-right font-mono">
                      {pct(s.share_of_net_pct)}
                    </TableCell>
                    <TableCell className="text-right font-mono">
                      {pct(s.best_day_share_of_own_pct)}
                    </TableCell>
                    <TableCell className={cn('text-right font-mono', pnlClass(s.net_ex_best_day))}>
                      {money(s.net_ex_best_day)}
                    </TableCell>
                    <TableCell
                      className={cn('text-right font-mono', pnlClass(s.net_ex_portfolio_best_day))}
                    >
                      {money(s.net_ex_portfolio_best_day)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
          <p className="text-xs text-muted-foreground">
            Counted over days with at least one closed trade, after estimated charges. A high share
            is not a failure on its own. Check whether the best days came from conditions that
            repeat. A share above 100% means the other days lost money, and a dash means total net
            profit is not above zero.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Risk and capital use</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid grid-cols-1 min-[480px]:grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
            <MetricCard
              label="Return on capital"
              value={pct(d.return_on_capital_pct, 2)}
              hint={`on ${money(d.capital_base)} assumed`}
            />
            <MetricCard
              label="Max drawdown (after charges)"
              value={money(Math.abs(d.max_drawdown))}
              negative
              hint={pct(d.max_drawdown_pct, 2)}
            />
            <MetricCard
              label="Recovery factor"
              value={num(d.recovery_factor)}
              hint={`per trade ${num(d.recovery_factor_trade_sequence)}`}
            />
            <MetricCard
              label="Drawdown length"
              value={
                d.drawdown_duration_sessions == null
                  ? dash
                  : `${d.drawdown_duration_sessions} sessions`
              }
              hint={d.drawdown_recovered === false ? 'not yet recovered' : undefined}
            />
            <MetricCard
              label="Calmar"
              value={num(d.calmar_ratio)}
              hint={d.calmar_ratio == null ? 'needs 60 sessions' : undefined}
            />
            <MetricCard
              label="Longest losing run"
              value={`${insights.streaks.max_consecutive_losses} trades`}
              hint={`average run ${num(insights.streaks.avg_losing_streak)}`}
            />
          </div>
          <div className="grid grid-cols-1 min-[480px]:grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
            <MetricCard label="Gross profit factor" value={num(insights.gross_profit_factor)} />
            <MetricCard label="Net profit factor" value={num(insights.net_profit_factor)} />
            <MetricCard
              label="Average peak deployed"
              value={
                insights.deployment.avg_daily_peak_deployed == null
                  ? dash
                  : money(insights.deployment.avg_daily_peak_deployed)
              }
            />
            <MetricCard
              label="Largest deployed"
              value={
                insights.deployment.max_deployed == null
                  ? dash
                  : money(insights.deployment.max_deployed)
              }
            />
          </div>
          <p className="text-xs text-muted-foreground">
            Drawdown is on realized profit after charges at each session close, starting from zero;
            positions still open are not marked to market. The per-trade recovery factor uses the
            order trades closed in. Percentages use the reference capital the recorded trades were
            sized on, so they do not change with the capital box. Deployed capital is the entry
            value of positions held at the same time, not margin
            {insights.deployment.deployment_excluded > 0
              ? `, and ${insights.deployment.deployment_excluded} trades with unreliable times are left out`
              : ''}
            .
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Is the edge holding up?</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 md:grid-cols-2">
            <Rolling
              title="All strategies"
              points={insights.rolling.combined}
              money={money}
              window={insights.rolling.expectancy_window}
              field="exp20"
            />
            <Rolling
              title="All strategies, profit factor"
              points={insights.rolling.combined}
              money={money}
              window={insights.rolling.pf_window}
              field="pf50"
            />
            {names.length > 1 &&
              names.map((n) => (
                <Rolling
                  key={n}
                  title={n}
                  points={insights.rolling.by_strategy[n]}
                  money={money}
                  window={insights.rolling.expectancy_window}
                  field="exp20"
                />
              ))}
          </div>
          <p className="text-xs text-muted-foreground">
            Expectancy is the average net result of the last {insights.rolling.expectancy_window}{' '}
            trades; profit factor uses the last {insights.rolling.pf_window}. A series appears once
            there are enough trades. The dashed line is zero.
          </p>
          {insights.monthly.length > 0 && (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Month</TableHead>
                  <TableHead className="text-right">Trades</TableHead>
                  <TableHead className="text-right">Traded days</TableHead>
                  <TableHead className="text-right">Net P&amp;L</TableHead>
                  <TableHead className="text-right">Win rate</TableHead>
                  <TableHead className="text-right">Net PF</TableHead>
                  <TableHead className="text-right">Profitable days</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {insights.monthly.map((m) => (
                  <TableRow key={m.month}>
                    <TableCell>{m.month}</TableCell>
                    <TableCell className="text-right font-mono">{m.trades}</TableCell>
                    <TableCell className="text-right font-mono">{m.traded_days}</TableCell>
                    <TableCell className={cn('text-right font-mono', pnlClass(m.net))}>
                      {money(m.net)}
                    </TableCell>
                    <TableCell className="text-right font-mono">{pct(m.win_rate)}</TableCell>
                    <TableCell className="text-right font-mono">{num(m.profit_factor)}</TableCell>
                    <TableCell className="text-right font-mono">
                      {pct(m.profitable_days_pct)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Entry quality</CardTitle>
        </CardHeader>
        <CardContent className="space-y-5">
          <BucketTable title="First hour or later" rows={eq.by_session_part} money={money} />
          <BucketTable title="By entry hour" rows={eq.by_hour} money={money} />
          <BucketTable title="By weekday" rows={eq.by_weekday} money={money} />
          <BucketTable title="Long or short" rows={eq.by_direction} money={money} />
          <p className="text-xs text-muted-foreground">
            All figures are after estimated charges. The first hour ends at {eq.first_hour_ends}.
            Small groups prove little: read a row with only a handful of trades as a hint, not a
            finding.
            {eq.entry_excluded > 0
              ? ` ${eq.entry_excluded} trades with no reliable entry time are left out of the hour and first-hour tables.`
              : ''}
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Execution cost</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {sl.covered === 0 ? (
            <p className="text-sm text-muted-foreground">
              No trade could be matched to its signal, so entry slippage is not measured here.
            </p>
          ) : (
            <>
              <div className="grid grid-cols-1 min-[480px]:grid-cols-2 sm:grid-cols-4 gap-3">
                <MetricCard
                  label="Net entry slippage"
                  value={money(sl.net ?? 0)}
                  hint={`${num(sl.net_bps)} bps`}
                />
                <MetricCard
                  label="Worse than the alert"
                  value={money(sl.adverse_total ?? 0)}
                  hint={`${sl.adverse_trades} trades, ${num(sl.adverse_bps)} bps`}
                />
                <MetricCard
                  label="Better than the alert"
                  value={money(Math.abs(sl.favourable_total ?? 0))}
                  hint={`${sl.favourable_trades} trades`}
                />
                <MetricCard
                  label="Measured on"
                  value={`${sl.covered} of ${sl.total}`}
                  hint="trades"
                />
              </div>
              {Object.keys(sl.by_strategy).length > 1 && (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Strategy</TableHead>
                      <TableHead className="text-right">Net</TableHead>
                      <TableHead className="text-right">Worse than alert</TableHead>
                      <TableHead className="text-right">Better than alert</TableHead>
                      <TableHead className="text-right">Net bps</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {Object.entries(sl.by_strategy).map(([name, s]) => (
                      <TableRow key={name}>
                        <TableCell>{name}</TableCell>
                        <TableCell className="text-right font-mono">{money(s.net)}</TableCell>
                        <TableCell className="text-right font-mono">
                          {money(s.adverse_total)}
                        </TableCell>
                        <TableCell className="text-right font-mono">
                          {money(Math.abs(s.favourable_total))}
                        </TableCell>
                        <TableCell className="text-right font-mono">{num(s.net_bps)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
              {sl.by_symbol.length > 0 && (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Symbols with the largest adverse slippage</TableHead>
                      <TableHead className="text-right">Worse than alert</TableHead>
                      <TableHead className="text-right">Net</TableHead>
                      <TableHead className="text-right">Trades</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {sl.by_symbol.slice(0, 5).map((s) => (
                      <TableRow key={s.symbol}>
                        <TableCell>{s.symbol}</TableCell>
                        <TableCell className="text-right font-mono">
                          {money(s.adverse_total)}
                        </TableCell>
                        <TableCell className="text-right font-mono">{money(s.net)}</TableCell>
                        <TableCell className="text-right font-mono">{s.trades}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </>
          )}
          <p className="text-xs text-muted-foreground">
            Slippage here is the entry fill against the price in the alert, already inside the
            profit and loss shown. It does not include exit slippage, and fills in Sandbox mode are
            simulated, so it is not a measure of live execution. Charges throughout are estimates,
            not amounts your broker reported.
          </p>
        </CardContent>
      </Card>
    </div>
  )
}

function ExcursionRow({ name, e }: { name: string; e: ExcursionBlock }) {
  return (
    <TableRow>
      <TableCell>{name}</TableCell>
      <TableCell className="text-right font-mono">{e.trades}</TableCell>
      <TableCell className="text-right font-mono">{num(e.avg_mfe_r)}R</TableCell>
      <TableCell className="text-right font-mono">{num(e.avg_mae_r)}R</TableCell>
      <TableCell className="text-right font-mono">{pct(e.mfe_capture_pct)}</TableCell>
      <TableCell className="text-right font-mono">
        {e.mfe_r_quartiles ? e.mfe_r_quartiles.map((v) => v.toFixed(2)).join(' / ') : dash}
      </TableCell>
      <TableCell className="text-right font-mono">{pct(e.mae_reached_stop_pct)}</TableCell>
    </TableRow>
  )
}

/** Loaded on its own: it reads price bars for every trade, so it is not part of the daily report. */
export function ExcursionCard({
  strategy,
  period,
  range,
  enabled,
}: {
  strategy: StrategySelection
  period: PerformancePeriod
  range?: DateRange
  enabled: boolean
}) {
  const query = useQuery({
    queryKey: ['strategy-daily-excursions', strategy, period, range?.start, range?.end],
    queryFn: () => strategyPnlApi.excursions(strategy, period, range),
    enabled,
    staleTime: 5 * 60 * 1000,
  })
  const e = query.data?.excursions
  return (
    <Card>
      <CardHeader>
        <CardTitle>Exit quality (best and worst point of each trade)</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {query.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Reading price bars for each trade
          </div>
        ) : query.isError ? (
          <p className="text-sm text-muted-foreground">
            This could not be loaded just now. Try the refresh button.
          </p>
        ) : !e ? (
          <p className="text-sm text-muted-foreground">
            {query.data?.message ?? 'No excursion data for this selection.'}
          </p>
        ) : (
          <>
            <div className="grid grid-cols-1 min-[480px]:grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
              <MetricCard
                label="Average best move"
                value={`${num(e.avg_mfe_r)}R`}
                hint="in units of initial risk"
              />
              <MetricCard
                label="Average worst move"
                value={`${num(e.avg_mae_r)}R`}
                hint="against the trade"
              />
              <MetricCard
                label="Share of best move kept"
                value={pct(e.mfe_capture_pct)}
                hint="winners only"
              />
              <MetricCard
                label={`Gave back after +${e.meaningful_mfe_r}R`}
                value={pct(e.gave_back_pct)}
                hint={`of ${e.reached_meaningful_mfe} trades that got there, closed at a loss`}
              />
              <MetricCard label="Winners, worst move" value={`${num(e.winners_avg_mae_r)}R`} />
              <MetricCard
                label="Losers, worst move"
                value={`${num(e.losers_avg_mae_r)}R`}
                hint={`${pct(e.mae_reached_stop_pct)} reached the full stop distance`}
              />
            </div>
            {Object.keys(e.by_strategy).length > 0 && (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Strategy</TableHead>
                    <TableHead className="text-right">Trades</TableHead>
                    <TableHead className="text-right">Avg best move</TableHead>
                    <TableHead className="text-right">Avg worst move</TableHead>
                    <TableHead className="text-right">Winner profit kept</TableHead>
                    <TableHead className="text-right">Best move spread (25 / 50 / 75%)</TableHead>
                    <TableHead className="text-right">Reached full stop</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {Object.entries(e.by_strategy).map(([name, b]) => (
                    <ExcursionRow key={name} name={name} e={b} />
                  ))}
                </TableBody>
              </Table>
            )}
            <p className="text-xs text-muted-foreground">
              {e.covered} of {e.total} trades had price bars
              {e.excluded_flagged > 0
                ? `; ${e.excluded_flagged} with unreliable times are left out`
                : ''}
              . R needs the stop price from the alert, so {e.r_trades} of {e.trades} trades appear
              in the R figures. The best and worst points come from one-minute highs and lows, so
              they are upper bounds: the market may have touched them where your quantity could not
              have been filled.
            </p>
          </>
        )}
      </CardContent>
    </Card>
  )
}
