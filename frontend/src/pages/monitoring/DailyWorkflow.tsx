import { useQuery } from '@tanstack/react-query'
import {
  AlertTriangle,
  CalendarCheck,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleDashed,
  Clock,
  FileText,
  RefreshCw,
  XCircle,
} from 'lucide-react'
import { useMemo, useState } from 'react'
import Markdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  opsWorkflowApi,
  type WorkflowCheck,
  type WorkflowCheckResult,
  type WorkflowDay,
  type WorkflowPhase,
  type WorkflowStep,
} from '@/api/opsWorkflow'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'

type PhaseKey = WorkflowPhase['phase']
type Tone = 'good' | 'warn' | 'bad' | 'idle'

const PHASES: { key: PhaseKey; title: string; when: string; duty: string; checks: string[] }[] = [
  {
    key: 'open-day',
    title: 'Open',
    when: '09:12',
    duty: 'Is the stack ready to trade',
    checks: ['open-day'],
  },
  {
    key: 'liveness',
    title: 'After the open',
    when: '09:27',
    duty: 'Is the engine actually trading',
    checks: ['liveness'],
  },
  {
    key: 'close-day',
    title: 'Close',
    when: '15:36',
    duty: 'Books, data, errors and jobs, then the review',
    checks: ['logs', 'value-areas', 'jobs'],
  },
]

const STEP_INFO: Record<string, string> = {
  check: 'Run the checks',
  notify: 'Telegram message (only when needed)',
  backfill: "Download the day's 1-minute and daily bars",
  eod: 'Tradebook snapshot, ledger, P&L pages vs fills',
  execution_health: 'Order and broker health',
  logs: 'Errors grouped against the last 5 sessions',
  weekly: 'Weekly roll-up (Fridays)',
  value_areas: "Store the day's VAH / POC / VAL",
  jobs: 'Did every scheduled job run',
  review: 'Close-day review',
}

const CHECK_TITLES: Record<string, string> = {
  'open-day': 'Readiness',
  liveness: 'Liveness',
  logs: 'Errors today',
  'value-areas': 'Value areas',
  jobs: 'Scheduled jobs',
}

const REPORT_NAMES: [RegExp, string][] = [
  [/^eod-.*-claude-review\.md$/, 'Close-day review'],
  [/^eod-\d{4}-\d{2}-\d{2}\.md$/, 'EOD report'],
  [/^execution-health-/, 'Execution health'],
  [/^logs-/, 'Errors'],
  [/^jobs-/, 'Scheduled jobs'],
  [/^value-areas-/, 'Value areas'],
  [/^open-day-/, 'Open checks'],
  [/^liveness-/, 'After-open checks'],
  [/^weekly-/, 'Weekly roll-up'],
]

const TONE_CLASS: Record<Tone, string> = {
  good: 'bg-green-600 text-white',
  warn: 'bg-amber-500 text-white',
  bad: 'bg-red-600 text-white',
  idle: 'bg-muted text-muted-foreground',
}

const STATUS_ORDER: Record<WorkflowCheck['status'], number> = {
  FAIL: 0,
  WARN: 1,
  'NOT VERIFIED': 2,
  OK: 3,
}

function verdictTone(verdict: string | undefined): Tone {
  if (verdict === 'GREEN' || verdict === 'OK') return 'good'
  if (verdict === 'RED' || verdict === 'FAIL') return 'bad'
  if (verdict === undefined) return 'idle'
  return 'warn'
}

function reportName(file: string): string {
  return REPORT_NAMES.find(([pattern]) => pattern.test(file))?.[1] ?? file
}

function seconds(hms: string): number {
  const [h, m, s] = hms.split(':').map(Number)
  return h * 3600 + m * 60 + s
}

function duration(step: WorkflowStep): string {
  const total = Math.max(0, seconds(step.ended) - seconds(step.started))
  return total >= 60 ? `${Math.floor(total / 60)}m ${total % 60}s` : `${total}s`
}

function phaseTone(phase: WorkflowPhase | undefined, day: WorkflowDay, checks: string[]): Tone {
  if (!phase?.steps) return 'idle'
  if (phase.steps.some((s) => s.exit !== 0)) return 'bad'
  const verdicts = checks.map((c) => day.checks[c]?.verdict).filter(Boolean)
  if (verdicts.includes('RED')) return 'bad'
  if (verdicts.includes('AMBER')) return 'warn'
  return 'good'
}

function dayTone(day: WorkflowDay): Tone {
  if (day.verdict) return verdictTone(day.verdict.verdict)
  const tones = PHASES.map((p) =>
    phaseTone(
      day.phases.find((x) => x.phase === p.key),
      day,
      p.checks
    )
  )
  if (tones.includes('bad')) return 'bad'
  if (tones.includes('warn')) return 'warn'
  return tones.includes('good') ? 'good' : 'idle'
}

function ToneIcon({ tone, className = 'h-4 w-4' }: { tone: Tone; className?: string }) {
  if (tone === 'good') return <CheckCircle2 className={`${className} text-green-600`} />
  if (tone === 'bad') return <XCircle className={`${className} text-red-600`} />
  if (tone === 'warn') return <AlertTriangle className={`${className} text-amber-500`} />
  return <CircleDashed className={`${className} text-muted-foreground`} />
}

function Summary({ day }: { day: WorkflowDay }) {
  const tone = dayTone(day)
  const allChecks = Object.values(day.checks).flatMap((c) => c.checks)
  const failedSteps = day.phases.flatMap((p) => (p.steps ?? []).filter((s) => s.exit !== 0))
  const tiles = [
    {
      label: 'Phases run',
      value: `${day.phases.filter((p) => p.steps !== null).length} / ${PHASES.length}`,
    },
    { label: 'Steps failed', value: failedSteps.length },
    { label: 'Checks failing', value: allChecks.filter((c) => c.status === 'FAIL').length },
    { label: 'Checks to look at', value: allChecks.filter((c) => c.status === 'WARN').length },
  ]
  const label = { good: 'GREEN', warn: 'AMBER', bad: 'RED', idle: 'NO RUN' }[tone]
  return (
    <Card>
      <CardContent className="pt-6 flex flex-col lg:flex-row gap-6 lg:items-center">
        <div className="flex items-start gap-3 lg:w-1/2">
          <ToneIcon tone={tone} className="h-8 w-8 shrink-0" />
          <div>
            <div className="flex items-center gap-2">
              <Badge className={TONE_CLASS[tone]}>{label}</Badge>
              <span className="text-sm text-muted-foreground">{day.day}</span>
            </div>
            <p className="mt-2 text-sm">
              {day.verdict?.line ??
                'No close-day verdict yet. It is written by the review after 15:36.'}
            </p>
          </div>
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 lg:w-1/2">
          {tiles.map((t) => (
            <div key={t.label} className="rounded-md border p-3">
              <div className="text-xl font-semibold">{t.value}</div>
              <div className="text-xs text-muted-foreground">{t.label}</div>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  )
}

function Timeline({
  day,
  selected,
  onSelect,
}: {
  day: WorkflowDay
  selected: PhaseKey
  onSelect: (p: PhaseKey) => void
}) {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
      {PHASES.map((p) => {
        const phase = day.phases.find((x) => x.phase === p.key)
        const tone = phaseTone(phase, day, p.checks)
        const active = selected === p.key
        return (
          <button
            type="button"
            key={p.key}
            onClick={() => onSelect(p.key)}
            className={`text-left rounded-lg border p-4 transition-colors ${
              active ? 'border-primary ring-1 ring-primary bg-primary/5' : 'hover:bg-muted/50'
            }`}
          >
            <div className="flex items-center justify-between">
              <span className="font-semibold">{p.title}</span>
              <ToneIcon tone={tone} />
            </div>
            <div className="mt-1 flex items-center gap-1 text-xs text-muted-foreground">
              <Clock className="h-3 w-3" />
              {p.when}
              {phase?.steps?.length ? ` - ran ${phase.steps[0].started}` : ' - not run'}
            </div>
            <div className="mt-2 text-xs text-muted-foreground">{p.duty}</div>
          </button>
        )
      })}
    </div>
  )
}

function Steps({ phase }: { phase: WorkflowPhase | undefined }) {
  if (!phase?.steps) {
    return <p className="text-sm text-muted-foreground">This phase did not run on this day.</p>
  }
  return (
    <ol className="space-y-2">
      {phase.steps.map((s) => {
        const tone: Tone = s.exit === 0 ? 'good' : 'bad'
        return (
          <li key={s.step} className="flex items-start gap-3 rounded-md border p-2">
            <ToneIcon tone={tone} className="h-4 w-4 mt-0.5 shrink-0" />
            <div className="flex-1 min-w-0">
              <div className="flex justify-between gap-2">
                <span className="text-sm font-medium">{STEP_INFO[s.step] ?? s.step}</span>
                <span className="text-xs text-muted-foreground whitespace-nowrap">
                  {s.started} | {duration(s)}
                </span>
              </div>
              {s.exit !== 0 && (
                <div className="text-xs text-red-600">
                  {s.exit === 124 ? 'Stopped at the deadline' : `Failed (exit ${s.exit})`}
                </div>
              )}
            </div>
          </li>
        )
      })}
    </ol>
  )
}

function CheckGroup({ name, result }: { name: string; result: WorkflowCheckResult }) {
  const [showOk, setShowOk] = useState(false)
  const sorted = [...result.checks].sort((a, b) => STATUS_ORDER[a.status] - STATUS_ORDER[b.status])
  const attention = sorted.filter((c) => c.status !== 'OK')
  const ok = sorted.filter((c) => c.status === 'OK')
  return (
    <div className="rounded-lg border">
      <div className="flex items-center justify-between gap-2 border-b px-4 py-3">
        <div>
          <div className="font-medium">{CHECK_TITLES[name] ?? name}</div>
          <div className="text-xs text-muted-foreground">{result.summary}</div>
        </div>
        <Badge className={TONE_CLASS[verdictTone(result.verdict)]}>{result.verdict}</Badge>
      </div>
      <ul className="divide-y">
        {attention.map((c) => (
          <li key={`${c.name}-${c.evidence}`} className="flex gap-3 px-4 py-2">
            <ToneIcon tone={verdictTone(c.status)} className="h-4 w-4 mt-0.5 shrink-0" />
            <div className="min-w-0">
              <div className="text-sm font-medium">{c.name}</div>
              <div className="text-xs text-muted-foreground break-words">{c.evidence}</div>
            </div>
          </li>
        ))}
      </ul>
      {ok.length > 0 && (
        <div className="px-4 py-2 border-t">
          <button
            type="button"
            className="text-xs text-muted-foreground hover:text-foreground"
            onClick={() => setShowOk(!showOk)}
          >
            {showOk ? 'Hide' : 'Show'} {ok.length} checks that passed
          </button>
          {showOk && (
            <ul className="mt-2 space-y-1">
              {ok.map((c) => (
                <li key={`${c.name}-${c.evidence}`} className="text-xs text-muted-foreground">
                  <span className="text-foreground">{c.name}</span>: {c.evidence}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}

const MARKDOWN: Components = {
  h1: ({ children }) => <h2 className="text-lg font-semibold mt-4 mb-2">{children}</h2>,
  h2: ({ children }) => <h3 className="text-base font-semibold mt-4 mb-2">{children}</h3>,
  h3: ({ children }) => <h4 className="text-sm font-semibold mt-3 mb-1">{children}</h4>,
  p: ({ children }) => <p className="text-sm my-2 leading-relaxed">{children}</p>,
  ul: ({ children }) => <ul className="list-disc pl-5 text-sm space-y-1 my-2">{children}</ul>,
  ol: ({ children }) => <ol className="list-decimal pl-5 text-sm space-y-1 my-2">{children}</ol>,
  table: ({ children }) => (
    <div className="overflow-x-auto my-3">
      <table className="w-full text-xs border-collapse">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border px-2 py-1 text-left font-medium bg-muted/50">{children}</th>
  ),
  td: ({ children }) => <td className="border px-2 py-1 align-top">{children}</td>,
  pre: ({ children }) => (
    <pre className="text-xs bg-muted/60 rounded-md p-3 overflow-x-auto my-2">{children}</pre>
  ),
  code: ({ children }) => <code className="text-xs font-mono">{children}</code>,
  blockquote: ({ children }) => (
    <blockquote className="border-l-2 pl-3 text-sm text-muted-foreground my-2">
      {children}
    </blockquote>
  ),
}

function Reports({ reports }: { reports: string[] }) {
  const ordered = useMemo(
    () =>
      [...reports].sort(
        (a, b) =>
          REPORT_NAMES.findIndex(([p]) => p.test(a)) - REPORT_NAMES.findIndex(([p]) => p.test(b))
      ),
    [reports]
  )
  const [chosen, setChosen] = useState<string | null>(null)
  const current = chosen && ordered.includes(chosen) ? chosen : (ordered[0] ?? null)
  const report = useQuery({
    queryKey: ['ops-report', current],
    queryFn: () => opsWorkflowApi.report(current ?? ''),
    enabled: current !== null,
  })
  if (ordered.length === 0) return null
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base flex items-center gap-2">
          <FileText className="h-4 w-4" />
          Reports
        </CardTitle>
      </CardHeader>
      <CardContent className="grid grid-cols-1 lg:grid-cols-4 gap-4">
        <nav className="flex lg:flex-col gap-1 overflow-x-auto">
          {ordered.map((name) => (
            <button
              type="button"
              key={name}
              onClick={() => setChosen(name)}
              className={`text-left whitespace-nowrap rounded-md px-3 py-2 text-sm ${
                name === current ? 'bg-primary text-primary-foreground' : 'hover:bg-muted'
              }`}
            >
              {reportName(name)}
            </button>
          ))}
        </nav>
        <div className="lg:col-span-3 min-w-0 rounded-md border p-4 max-h-[75vh] overflow-y-auto">
          {report.isLoading ? (
            <Skeleton className="h-48 w-full" />
          ) : report.error ? (
            <p className="text-sm">{(report.error as Error).message}</p>
          ) : (
            <Markdown remarkPlugins={[remarkGfm]} skipHtml components={MARKDOWN}>
              {report.data ?? ''}
            </Markdown>
          )}
        </div>
      </CardContent>
    </Card>
  )
}

function lastRunPhase(day: WorkflowDay): PhaseKey {
  const ran = PHASES.filter((p) => day.phases.find((x) => x.phase === p.key)?.steps)
  return ran.length ? ran[ran.length - 1].key : 'close-day'
}

export default function DailyWorkflow() {
  const [day, setDay] = useState<string | undefined>(undefined)
  const [phase, setPhase] = useState<PhaseKey | null>(null)
  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ['ops-workflow', day],
    queryFn: () => opsWorkflowApi.get(day),
  })
  const view = data?.data
  const days = data?.days ?? []
  const index = view ? days.indexOf(view.day) : -1
  const selected = phase ?? (view ? lastRunPhase(view) : 'close-day')
  const meta = PHASES.find((p) => p.key === selected) ?? PHASES[2]

  const pick = (d: string) => {
    setDay(d)
    setPhase(null)
  }

  return (
    <div className="py-6 space-y-6">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <CalendarCheck className="h-6 w-6" />
            Daily Workflow
          </h1>
          <p className="text-muted-foreground mt-1">
            The day's three checkpoints: before the open, after the open and at the close
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="icon"
            disabled={index < 0 || index >= days.length - 1}
            onClick={() => pick(days[index + 1])}
            aria-label="Previous day"
          >
            <ChevronLeft className="h-4 w-4" />
          </Button>
          {days.length > 0 && (
            <Select value={view?.day} onValueChange={pick}>
              <SelectTrigger className="w-36">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {days.map((d) => (
                  <SelectItem key={d} value={d}>
                    {d}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          <Button
            variant="outline"
            size="icon"
            disabled={index <= 0}
            onClick={() => pick(days[index - 1])}
            aria-label="Next day"
          >
            <ChevronRight className="h-4 w-4" />
          </Button>
          <Button variant="outline" size="sm" onClick={() => refetch()} disabled={isFetching}>
            <RefreshCw className={`h-4 w-4 mr-2 ${isFetching ? 'animate-spin' : ''}`} />
            Refresh
          </Button>
        </div>
      </div>

      {isLoading ? (
        <Skeleton className="h-64 w-full" />
      ) : error ? (
        <Card>
          <CardContent className="pt-6 text-sm">{(error as Error).message}</CardContent>
        </Card>
      ) : !view || !view.day ? (
        <Card>
          <CardContent className="pt-6 text-sm text-muted-foreground">
            No workflow runs yet. The first runs at 09:12 on the next trading day.
          </CardContent>
        </Card>
      ) : (
        <>
          <Summary day={view} />
          <Timeline day={view} selected={selected} onSelect={setPhase} />

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base">
                {meta.title} <span className="font-normal text-muted-foreground">{meta.when}</span>
              </CardTitle>
            </CardHeader>
            <CardContent className="grid grid-cols-1 lg:grid-cols-3 gap-6">
              <div>
                <div className="text-xs font-semibold uppercase text-muted-foreground mb-2">
                  Steps
                </div>
                <Steps phase={view.phases.find((p) => p.phase === selected)} />
              </div>
              <div className="lg:col-span-2 space-y-4">
                <div className="text-xs font-semibold uppercase text-muted-foreground">
                  Findings
                </div>
                {meta.checks.filter((c) => view.checks[c]).length === 0 ? (
                  <p className="text-sm text-muted-foreground">
                    No checks recorded for this phase on this day.
                  </p>
                ) : (
                  meta.checks
                    .filter((c) => view.checks[c])
                    .map((c) => <CheckGroup key={c} name={c} result={view.checks[c]} />)
                )}
              </div>
            </CardContent>
          </Card>

          <Reports key={view.day} reports={view.reports} />
        </>
      )}
    </div>
  )
}
