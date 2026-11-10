import type { ReactNode } from 'react'
import type { PortfolioView, RunDetail, RunRecord, Verdict } from '@/api/backtestResults'
import { fmtNum, fmtRupees, fmtSigned, isKnown, metricText, rupeePnl } from '@/lib/backtestResults'
import { buildDecisionBrief } from '@/lib/decisionBrief'
import { decisionNote, researchDecision, type SummaryIdea } from '@/lib/runSummary'
import { cn } from '@/lib/utils'
import { PlainVerdictLine, verdictToneClass } from './plainVerdict'

function signTone(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return ''
  return value > 0 ? 'text-green-600' : value < 0 ? 'text-red-600' : ''
}

/** Says over how long the total return was earned, so a ten-year total is not read as a one-year gain. */
function returnHint(stats: PortfolioView['stats']): string {
  if (typeof stats?.total_return_pct !== 'number') return 'No portfolio book for this run'
  const years = typeof stats.years === 'number' ? ` over ${fmtNum(stats.years, 1)} years` : ''
  const annual =
    typeof stats.annual_return_pct === 'number'
      ? `, ${fmtSigned(stats.annual_return_pct, 1)}% a year`
      : ''
  return `Whole portfolio${years}${annual}`
}

function Card({
  label,
  value,
  hint,
  tone,
  testId,
}: {
  label: string
  value: string
  hint?: string
  tone?: string
  testId: string
}) {
  return (
    <div className="rounded-md border p-3" data-testid={testId}>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={cn('font-mono text-xl font-semibold', tone)}>{value}</div>
      {hint && <div className="text-xs text-muted-foreground">{hint}</div>}
    </div>
  )
}

/**
 * The six figures that answer "how did it do and how much risk did it take". Every value is read
 * from the run as recorded. A figure the run does not carry reads "n/a" with the reason, never 0.
 */
export function KeyMetrics({
  run,
  portfolio,
}: {
  run: RunRecord
  portfolio?: PortfolioView | null
}) {
  const m = run.metrics
  const rupees = rupeePnl(run)
  const stats = portfolio && !portfolio.unavailable ? portfolio.stats : undefined
  const ret = stats?.total_return_pct
  const dd = stats?.max_drawdown_pct
  return (
    <section aria-label="Key results" data-testid="key-metrics">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
        <Card
          testId="kpi-net-pnl"
          label="Net P&L after costs"
          value={rupees ? fmtRupees(rupees.net_rs, true) : 'n/a'}
          hint={
            rupees
              ? rupees.total_cost_rs !== null
                ? `Costs ${fmtRupees(rupees.total_cost_rs)}`
                : undefined
              : 'No rupee result recorded for this run'
          }
          tone={signTone(rupees?.net_rs)}
        />
        <Card
          testId="kpi-return"
          label="Return"
          value={typeof ret === 'number' ? `${fmtSigned(ret, 1)}%` : 'n/a'}
          hint={returnHint(stats)}
          tone={signTone(ret)}
        />
        <Card
          testId="kpi-drawdown"
          label="Max drawdown"
          value={
            typeof dd === 'number'
              ? `${fmtNum(dd, 1)}%`
              : metricText(m, m.max_dd_r, (v) => `${fmtNum(v, 1)}R`)
          }
          hint={
            typeof dd !== 'number'
              ? 'In R: no portfolio book'
              : stats?.max_drawdown_peak && stats.max_drawdown_trough
                ? `${stats.max_drawdown_peak} to ${stats.max_drawdown_trough}`
                : undefined
          }
        />
        <Card
          testId="kpi-profit-factor"
          label="Profit factor"
          value={metricText(m, m.profit_factor, (v) => fmtNum(v))}
        />
        <Card
          testId="kpi-trades"
          label="Trades"
          value={m.trades === undefined ? 'n/a' : String(m.trades)}
        />
        <Card
          testId="kpi-avg-trade"
          label="Average trade"
          value={metricText(m, m.net_bps, (v) => `${fmtSigned(v, 0)} bps`)}
          hint="After costs; 100 bps = 1%"
          tone={isKnown(m, m.net_bps) ? signTone(m.net_bps) : ''}
        />
      </div>
    </section>
  )
}

/** One titled block of the report. The title is optional: the first card is the answer itself. */
export function ReportCard({
  title,
  children,
  testId,
}: {
  title?: string
  children: ReactNode
  testId?: string
}) {
  return (
    <section className="space-y-4 rounded-lg border bg-card p-4 md:p-5" data-testid={testId}>
      {title && <h2 className="text-base font-semibold">{title}</h2>}
      {children}
    </section>
  )
}

function Meta({ children }: { children: ReactNode }) {
  return <span className="whitespace-nowrap">{children}</span>
}

/** What the report is about: name, what the strategy does, and what was tested. */
export function ReportHeader({
  title,
  summary,
  run,
  detail,
}: {
  title: string
  summary?: string
  run: RunRecord
  detail?: RunDetail
}) {
  const scope = detail?.captured ? detail.scope : null
  const timeframe = scope?.timeframe ?? run.data.bar_size
  return (
    <section className="space-y-2" data-testid="report-header">
      <h1 className="text-2xl font-bold">{title}</h1>
      {summary && <p className="line-clamp-3 max-w-3xl text-sm">{summary}</p>}
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-sm text-muted-foreground">
        <Meta>{run.data.universe}</Meta>
        <Meta>{timeframe}</Meta>
        <Meta>
          {run.period.start} to {run.period.end}
        </Meta>
        <span title={`Run ${run.run_id}`} className="whitespace-nowrap">
          Last run {run.date}
        </span>
      </div>
    </section>
  )
}

/**
 * The whole answer in one card: the calculated verdict, the reasons behind it in plain words,
 * the next step, and the six figures. The reasons are the system's own findings and caveats, so
 * nothing here is a judgement made by the page. Gate ids and links stay in the validation view.
 */
export function ResultCard({
  idea,
  run,
  detail,
  portfolio,
  verdict,
  verdictIsLatestRecorded = false,
}: {
  idea: SummaryIdea
  run: RunRecord
  detail?: RunDetail
  portfolio?: PortfolioView | null
  /** Null when this run recorded no calculated verdict and is not the latest run. */
  verdict: Verdict | null
  /** The verdict is the idea's latest recorded one, not calculated for this run's detail yet. */
  verdictIsLatestRecorded?: boolean
}) {
  const brief = detail?.captured ? buildDecisionBrief(idea, run, detail) : null
  const decision = researchDecision(idea)
  const note = decisionNote(idea, verdict)
  return (
    <ReportCard testId="result-card">
      {verdict ? (
        <div className="space-y-1">
          <div className={cn('rounded-md border p-3', verdictToneClass(verdict))}>
            <PlainVerdictLine verdict={verdict} />
          </div>
          {verdictIsLatestRecorded && (
            <p className="text-xs text-muted-foreground" data-testid="verdict-provenance">
              Latest run verdict
            </p>
          )}
        </div>
      ) : (
        <p className="text-sm text-muted-foreground" data-testid="no-run-verdict">
          No calculated verdict was recorded for this run.
        </p>
      )}
      {brief && (brief.findings.length > 0 || brief.caveats.length > 0) && (
        <ul className="list-disc space-y-1 pl-5 text-sm" data-testid="why-list">
          {brief.findings.map((f) => (
            <li key={f.href}>{f.text}</li>
          ))}
          {brief.moreFailed > 0 && (
            <li>
              {brief.moreFailed} more failed test{brief.moreFailed === 1 ? '' : 's'} are in the
              validation details.
            </li>
          )}
          {brief.caveats.map((c) => (
            <li key={c} className="text-amber-700 dark:text-amber-400">
              {c}
            </li>
          ))}
        </ul>
      )}
      {decision && (
        <div className="space-y-1 rounded-md border p-3 text-sm" data-testid="research-decision">
          <p>
            <span className="font-semibold">Research decision:</span> {decision.label}
          </p>
          {note && (
            <p className="text-xs text-muted-foreground" data-testid="decision-note">
              {note}
            </p>
          )}
        </div>
      )}
      {brief && (
        <p className="text-sm" data-testid="next-step">
          <span className="font-semibold">Next step:</span> {brief.action.text}
          {brief.action.why && (
            <span className="block text-xs text-muted-foreground">{brief.action.why}</span>
          )}
        </p>
      )}
      <KeyMetrics run={run} portfolio={portfolio} />
      <p className="text-xs text-muted-foreground">
        Historical, after costs. A result is evidence to test forward, not a forecast.
      </p>
    </ReportCard>
  )
}
