import { useQuery } from '@tanstack/react-query'
import { FileText } from 'lucide-react'
import { useMemo, useState } from 'react'
import Markdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { opsWorkflowApi, type WorkflowCheckResult, type WorkflowDay } from '@/api/opsWorkflow'
import { StatusBadge, TONE_TEXT, type Tone } from '@/components/ops/StatusBadge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import {
  type ExecutionHealth,
  type ExecutionMetric,
  parseExecutionHealth,
  parseStrategyPnl,
  type StrategyPnlSummary,
} from '@/lib/reportParsers'
import { severityOfCheck } from '@/lib/workflowFindings'

export type ReportKey =
  | 'overview'
  | 'pnl'
  | 'execution'
  | 'errors'
  | 'jobs'
  | 'value'
  | 'open'
  | 'after'
  | 'weekly'

interface Section {
  key: ReportKey
  title: string
  file: RegExp
  /** Key in the day's check results that describes this report, when there is one. */
  check?: string
  /** The checkpoint that writes this report, so a missing one can say whether that checkpoint ran. */
  phase: 'open-day' | 'liveness' | 'close-day'
}

// Only sections whose report exists on the day are shown. Risk and data-quality have no report of
// their own; their findings appear under Needs attention.
const SECTIONS: Section[] = [
  { key: 'overview', title: 'Overview', file: /^eod-.*-claude-review\.md$/, phase: 'close-day' },
  { key: 'pnl', title: 'Strategy P&L', file: /^eod-\d{4}-\d{2}-\d{2}\.md$/, phase: 'close-day' },
  { key: 'execution', title: 'Execution Health', file: /^execution-health-/, phase: 'close-day' },
  { key: 'errors', title: 'Errors', file: /^logs-/, check: 'logs', phase: 'close-day' },
  { key: 'jobs', title: 'Scheduled Jobs', file: /^jobs-/, check: 'jobs', phase: 'close-day' },
  {
    key: 'value',
    title: 'Value Areas',
    file: /^value-areas-/,
    check: 'value-areas',
    phase: 'close-day',
  },
  { key: 'open', title: 'Open Checks', file: /^open-day-/, check: 'open-day', phase: 'open-day' },
  {
    key: 'after',
    title: 'After-open Checks',
    file: /^liveness-/,
    check: 'liveness',
    phase: 'liveness',
  },
  { key: 'weekly', title: 'Weekly roll-up', file: /^weekly-/, phase: 'close-day' },
]

export function reportFile(reports: string[], key: ReportKey): string | undefined {
  const section = SECTIONS.find((s) => s.key === key)
  return section ? reports.find((r) => section.file.test(r)) : undefined
}

export function availableSections(reports: string[]): Section[] {
  return SECTIONS.filter((s) => reports.some((r) => s.file.test(r)))
}

/** Weekly roll-up is written on Fridays only, so it is listed only on days that have it. */
export function listedSections(reports: string[]): Section[] {
  return SECTIONS.filter((s) => s.key !== 'weekly' || reports.some((r) => s.file.test(r)))
}

const MARKDOWN: Components = {
  h1: ({ children }) => <h2 className="text-lg font-semibold mt-4 mb-2">{children}</h2>,
  h2: ({ children }) => <h3 className="text-base font-semibold mt-4 mb-2">{children}</h3>,
  h3: ({ children }) => <h4 className="text-[15px] font-semibold mt-3 mb-1">{children}</h4>,
  p: ({ children }) => <p className="text-[15px] my-2 leading-relaxed">{children}</p>,
  ul: ({ children }) => <ul className="list-disc pl-5 text-[15px] space-y-1 my-2">{children}</ul>,
  ol: ({ children }) => (
    <ol className="list-decimal pl-5 text-[15px] space-y-1 my-2">{children}</ol>
  ),
  table: ({ children }) => (
    <div className="overflow-x-auto my-3">
      <table className="w-full text-[13px] border-collapse">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border px-2 py-1 text-left font-medium bg-muted/50">{children}</th>
  ),
  td: ({ children }) => <td className="border px-2 py-1 align-top">{children}</td>,
  pre: ({ children }) => (
    <pre className="text-[13px] bg-muted/60 rounded-md p-3 overflow-x-auto my-2">{children}</pre>
  ),
  code: ({ children }) => <code className="text-[13px] font-mono">{children}</code>,
  blockquote: ({ children }) => (
    <blockquote className="border-l-2 pl-3 text-[15px] text-muted-foreground my-2">
      {children}
    </blockquote>
  ),
}

const NOT_AVAILABLE = 'Not available'

function money(n: number): string {
  const text = Math.abs(n).toLocaleString('en-IN', {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  })
  return n < 0 ? `-${text}` : n > 0 ? `+${text}` : text
}

function moneyTone(n: number): string {
  return n < 0 ? TONE_TEXT.bad : n > 0 ? TONE_TEXT.good : ''
}

function Tile({
  label,
  value,
  sub,
  valueClass = '',
}: {
  label: string
  value: string
  sub?: string
  valueClass?: string
}) {
  return (
    <div className="rounded-md border px-3 py-2">
      <div className="text-[13px] text-muted-foreground">{label}</div>
      <div
        className={`text-xl font-semibold tabular-nums ${
          value === NOT_AVAILABLE
            ? 'text-[15px] text-muted-foreground font-normal py-1'
            : valueClass
        }`}
      >
        {value}
      </div>
      {sub && <div className="text-[13px] text-muted-foreground">{sub}</div>}
    </div>
  )
}

function Note({ children }: { children: React.ReactNode }) {
  return <p className="text-[13px] text-muted-foreground">{children}</p>
}

function Unavailable({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-md border border-dashed px-4 py-3 text-[15px] text-muted-foreground">
      <span className="font-medium text-foreground">{NOT_AVAILABLE}. </span>
      {children}
    </div>
  )
}

function PnlPanel({ data }: { data: StrategyPnlSummary | null }) {
  if (!data) {
    return (
      <Unavailable>
        This day's report has no strategy charges table. Older reports, and days where charges could
        not be modelled, do not include one. The full report below is the source.
      </Unavailable>
    )
  }
  const { strategies, total } = data
  const widest = Math.max(1, ...strategies.map((s) => Math.abs(s.net)))
  const reconTone: Tone =
    data.reconciliation === 'PASS' ? 'good' : data.reconciliation === 'FAIL' ? 'bad' : 'idle'
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 md:grid-cols-5 gap-2">
        <Tile
          label="Net P&L, all strategies"
          value={total ? money(total.net) : NOT_AVAILABLE}
          valueClass={total ? moneyTone(total.net) : ''}
          sub="after charges, INR"
        />
        <Tile
          label="Gross P&L"
          value={total ? money(total.gross) : NOT_AVAILABLE}
          valueClass={total ? moneyTone(total.gross) : ''}
        />
        <Tile
          label="Charges"
          value={
            total
              ? total.charges.toLocaleString('en-IN', { minimumFractionDigits: 1 })
              : NOT_AVAILABLE
          }
        />
        <Tile
          label="Trades"
          value={data.trades !== undefined ? String(data.trades) : NOT_AVAILABLE}
          sub="all strategies"
        />
        <Tile
          label="Win rate"
          value={data.winRatePct !== undefined ? `${data.winRatePct}%` : NOT_AVAILABLE}
          sub="all strategies, as reported"
        />
      </div>
      <div className="flex items-center gap-2 text-[15px]">
        <span className="text-muted-foreground">Strategy pages vs broker fills:</span>
        <StatusBadge tone={reconTone}>
          {data.reconciliation === 'PASS'
            ? 'Match'
            : data.reconciliation === 'FAIL'
              ? 'Differ'
              : 'Not reported'}
        </StatusBadge>
      </div>
      {strategies.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-[15px]">
            <thead>
              <tr className="border-b text-[13px] text-muted-foreground">
                <th className="py-1.5 text-left font-medium">Strategy</th>
                <th className="py-1.5 text-right font-medium">Gross</th>
                <th className="py-1.5 text-right font-medium">Charges</th>
                <th className="py-1.5 text-right font-medium">Net</th>
                <th className="py-1.5 pl-4 text-left font-medium w-1/4">Net, relative</th>
              </tr>
            </thead>
            <tbody>
              {strategies.map((s) => (
                <tr key={s.name} className="border-b last:border-0">
                  <td className="py-1.5 font-medium">{s.name}</td>
                  <td className={`py-1.5 text-right tabular-nums ${moneyTone(s.gross)}`}>
                    {money(s.gross)}
                  </td>
                  <td className="py-1.5 text-right tabular-nums">
                    {s.charges.toLocaleString('en-IN', { minimumFractionDigits: 1 })}
                  </td>
                  <td className={`py-1.5 text-right tabular-nums font-medium ${moneyTone(s.net)}`}>
                    {money(s.net)}
                  </td>
                  <td className="py-1.5 pl-4">
                    <div className="h-2 rounded bg-muted">
                      <div
                        className={`h-2 rounded ${s.net < 0 ? 'bg-red-600' : 'bg-green-600'}`}
                        style={{ width: `${(Math.abs(s.net) / widest) * 100}%` }}
                      />
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Note>
        Per-strategy trade count and win rate are not in the report, so they are not shown. The bar
        compares each strategy's net with the largest one that day.
      </Note>
    </div>
  )
}

interface KeyMetric {
  label: string
  match: RegExp
}

const KEY_METRICS: KeyMetric[] = [
  { label: 'API latency: entry signal to ack, p95 (s)', match: /signal-to-ack/i },
  { label: 'Rate limiter: longest wait, critical lane (s)', match: /limiter critical-lane/i },
  { label: 'Rejected orders (per-second)', match: /order rejections/i },
  { label: 'Calls refused by the rate limiter', match: /limiter refused/i },
  { label: 'Order timeouts', match: /timeout sending order/i },
  { label: 'Stale-price warnings', match: /stale-price/i },
  { label: 'Exit uncertain events', match: /exit uncertain/i },
  { label: 'Slow sandbox orders above 5s', match: /slow sandbox place_order/i },
]

function metricTone(status: string, metric: string): Tone {
  if (status === 'OK') return 'good'
  if (status === 'REGRESSION') return /exit uncertain/i.test(metric) ? 'bad' : 'warn'
  if (status === 'WATCH') return 'info'
  return 'idle'
}

const METRIC_LABEL: Record<string, string> = {
  OK: 'Normal',
  WATCH: 'Watch',
  REGRESSION: 'Regression',
}

function MetricStatus({ row }: { row: ExecutionMetric }) {
  return (
    <StatusBadge tone={metricTone(row.status, row.metric)}>
      {METRIC_LABEL[row.status] ?? row.status}
    </StatusBadge>
  )
}

function ExecutionPanel({ data }: { data: ExecutionHealth | null }) {
  const [showOk, setShowOk] = useState(false)
  if (!data) {
    return (
      <Unavailable>
        This report has no metrics table. The full report below is the source.
      </Unavailable>
    )
  }
  const rank = (s: string) => (s === 'REGRESSION' ? 0 : s === 'WATCH' ? 1 : 2)
  const rows = [...data.rows].sort((a, b) => rank(a.status) - rank(b.status))
  const visible = showOk ? rows : rows.filter((r) => r.status !== 'OK')
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2 text-[15px]">
        <span className="text-muted-foreground">Report conclusion:</span>
        {data.overall ? (
          <StatusBadge tone={data.overall === 'OK' ? 'good' : 'warn'}>{data.overall}</StatusBadge>
        ) : (
          <span className="text-muted-foreground">{NOT_AVAILABLE}</span>
        )}
      </div>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
        {KEY_METRICS.map((k) => {
          const row = data.rows.find((r) => k.match.test(r.metric))
          return (
            <div key={k.label} className="rounded-md border px-3 py-2">
              <div className="text-[13px] text-muted-foreground min-h-8">{k.label}</div>
              {row ? (
                <>
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-xl font-semibold tabular-nums">{row.today}</span>
                    <MetricStatus row={row} />
                  </div>
                  {row.prev !== undefined && (
                    <div className="text-[13px] text-muted-foreground">
                      Previous day: {row.prev}
                    </div>
                  )}
                </>
              ) : (
                <div className="text-[15px] text-muted-foreground py-1">{NOT_AVAILABLE}</div>
              )}
            </div>
          )
        })}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-[15px]">
          <thead>
            <tr className="border-b text-[13px] text-muted-foreground">
              <th className="py-1.5 text-left font-medium">Metric</th>
              <th className="py-1.5 text-right font-medium">Today</th>
              <th className="py-1.5 text-right font-medium">Previous day</th>
              <th className="py-1.5 pl-4 text-left font-medium">Status</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((r) => (
              <tr key={r.metric} className="border-b last:border-0">
                <td className="py-1.5">{r.metric}</td>
                <td className="py-1.5 text-right tabular-nums">{r.today}</td>
                <td className="py-1.5 text-right tabular-nums text-muted-foreground">
                  {r.prev ?? NOT_AVAILABLE}
                </td>
                <td className="py-1.5 pl-4">
                  <MetricStatus row={r} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <button
        type="button"
        className="text-[13px] text-muted-foreground hover:text-foreground"
        onClick={() => setShowOk(!showOk)}
      >
        {showOk ? 'Hide' : 'Show'} {data.rows.filter((r) => r.status === 'OK').length} metrics
        within limits
      </button>
    </div>
  )
}

const CHECK_TONE: Record<string, Tone> = {
  critical: 'bad',
  warning: 'warn',
  unverified: 'idle',
  info: 'good',
}
const CHECK_LABEL: Record<string, string> = {
  critical: 'Critical',
  warning: 'Warning',
  unverified: 'Unverified',
  info: 'Passed',
}

function ChecksPanel({ result }: { result: WorkflowCheckResult | undefined }) {
  if (!result) {
    return <Unavailable>No machine-readable check result was recorded for this report.</Unavailable>
  }
  const order = ['critical', 'warning', 'unverified', 'info']
  const checks = [...result.checks].sort(
    (a, b) => order.indexOf(severityOfCheck(a.status)) - order.indexOf(severityOfCheck(b.status))
  )
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-[15px]">
        <StatusBadge
          tone={result.verdict === 'GREEN' ? 'good' : result.verdict === 'RED' ? 'bad' : 'warn'}
        >
          {result.verdict}
        </StatusBadge>
        <span>{result.summary}</span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-[15px]">
          <thead>
            <tr className="border-b text-[13px] text-muted-foreground">
              <th className="py-1.5 text-left font-medium w-32">Result</th>
              <th className="py-1.5 text-left font-medium">Check</th>
              <th className="py-1.5 text-left font-medium">Evidence</th>
            </tr>
          </thead>
          <tbody>
            {checks.map((c) => {
              const sev = severityOfCheck(c.status)
              return (
                <tr key={`${c.name}-${c.evidence}`} className="border-b last:border-0 align-top">
                  <td className="py-1.5">
                    <StatusBadge tone={CHECK_TONE[sev]}>{CHECK_LABEL[sev]}</StatusBadge>
                  </td>
                  <td className="py-1.5 pr-3">{c.plain || c.name}</td>
                  <td className="py-1.5 text-[13px] text-muted-foreground break-words">
                    {c.name}: {c.evidence}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export function WorkflowReports({
  day,
  selected,
  onSelect,
}: {
  day: WorkflowDay
  selected: ReportKey | null
  onSelect: (key: ReportKey) => void
}) {
  const sections = useMemo(() => listedSections(day.reports), [day.reports])
  const hasReport = (key: ReportKey) => reportFile(day.reports, key) !== undefined
  const current =
    sections.find((s) => s.key === selected) ?? availableSections(day.reports)[0] ?? sections[0]
  const file = current ? reportFile(day.reports, current.key) : undefined
  const report = useQuery({
    queryKey: ['ops-report', file],
    queryFn: () => opsWorkflowApi.report(file ?? ''),
    enabled: file !== undefined,
  })
  if (!current) return null

  const text = report.data ?? ''
  const checkResult = current.check ? day.checks[current.check] : undefined
  const structured = ['pnl', 'execution', 'errors', 'jobs', 'value', 'open', 'after'].includes(
    current.key
  )

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base flex items-center gap-2">
          <FileText className="h-4 w-4" />
          Reports
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex gap-1 overflow-x-auto border-b pb-2" role="tablist">
          {sections.map((s) => {
            const verdict = s.check ? day.checks[s.check]?.verdict : undefined
            const tone: Tone | null =
              s.key === 'overview'
                ? day.verdict
                  ? day.verdict.verdict === 'GREEN'
                    ? 'good'
                    : day.verdict.verdict === 'RED'
                      ? 'bad'
                      : 'warn'
                  : null
                : verdict
                  ? verdict === 'GREEN'
                    ? 'good'
                    : verdict === 'RED'
                      ? 'bad'
                      : 'warn'
                  : null
            const missing = !hasReport(s.key)
            return (
              <button
                type="button"
                role="tab"
                aria-selected={s.key === current.key}
                key={s.key}
                onClick={() => onSelect(s.key)}
                title={missing ? 'No report was written for this day' : undefined}
                className={`flex items-center gap-1.5 whitespace-nowrap rounded-md px-3 py-1.5 text-[15px] ${
                  s.key === current.key
                    ? 'bg-primary text-primary-foreground'
                    : missing
                      ? 'text-muted-foreground hover:bg-muted'
                      : 'hover:bg-muted'
                }`}
              >
                {s.title}
                {missing && <span className="text-[13px] opacity-80">none</span>}
                {!missing && tone && (
                  <span
                    className={`inline-block h-2 w-2 rounded-full ${
                      {
                        good: 'bg-green-500',
                        warn: 'bg-amber-500',
                        bad: 'bg-red-500',
                        info: 'bg-blue-500',
                        idle: 'bg-muted-foreground',
                      }[tone]
                    }`}
                    role="img"
                    aria-label={`verdict ${tone === 'good' ? 'green' : tone === 'bad' ? 'red' : 'amber'}`}
                  />
                )}
              </button>
            )
          })}
        </div>

        {file === undefined ? (
          <Unavailable>
            No {current.title} report was written for {day.day}. {(() => {
              const ran = day.phases.find((p) => p.phase === current.phase)?.steps
              const name = { 'open-day': 'Open', liveness: 'After the open', 'close-day': 'Close' }[
                current.phase
              ]
              return ran
                ? `The ${name} checkpoint did run, so this report was not produced by it.`
                : `The ${name} checkpoint has not run on this day.`
            })()}
          </Unavailable>
        ) : report.isLoading ? (
          <Skeleton className="h-48 w-full" />
        ) : report.error ? (
          <p className="text-[15px]">{(report.error as Error).message}</p>
        ) : (
          <>
            {current.key === 'overview' && day.verdict && (
              <div className="flex flex-wrap items-center gap-2 text-[15px]">
                <StatusBadge
                  tone={
                    day.verdict.verdict === 'GREEN'
                      ? 'good'
                      : day.verdict.verdict === 'RED'
                        ? 'bad'
                        : 'warn'
                  }
                >
                  {day.verdict.verdict}
                </StatusBadge>
                <span>{day.verdict.line}</span>
              </div>
            )}
            {current.key === 'pnl' && <PnlPanel data={parseStrategyPnl(text)} />}
            {current.key === 'execution' && <ExecutionPanel data={parseExecutionHealth(text)} />}
            {['errors', 'jobs', 'value', 'open', 'after'].includes(current.key) && (
              <ChecksPanel result={checkResult} />
            )}
            <details open={!structured} className="rounded-md border">
              <summary className="cursor-pointer px-3 py-2 text-[15px] font-medium">
                Full report text
              </summary>
              <div className="border-t p-4 max-h-[70vh] overflow-y-auto">
                <Markdown remarkPlugins={[remarkGfm]} skipHtml components={MARKDOWN}>
                  {text}
                </Markdown>
              </div>
            </details>
          </>
        )}
      </CardContent>
    </Card>
  )
}
