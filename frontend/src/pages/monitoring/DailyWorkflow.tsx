import { useQueries, useQuery } from '@tanstack/react-query'
import {
  CalendarCheck,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Clock,
  RefreshCw,
} from 'lucide-react'
import { createContext, useContext, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { bugsApi } from '@/api/bugs'
import { type FindingLink, opsFindingsApi } from '@/api/opsFindings'
import { opsWorkflowApi, type WorkflowDay, type WorkflowStep } from '@/api/opsWorkflow'
import { ReportBugDialog } from '@/components/ops/ReportBugDialog'
import { StatusBadge, TONE_TEXT, type Tone, ToneIcon } from '@/components/ops/StatusBadge'
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
import { type BugSummary, groupOfStatus, summarizeBugs } from '@/lib/bugModel'
import { findingRef } from '@/lib/reportFinding'
import { parseExecutionHealth } from '@/lib/reportParsers'
import {
  ACTION_TEXT,
  buildFindings,
  CATEGORIES,
  countSeverities,
  defaultPhase,
  type Finding,
  type Health,
  healthOf,
  metricFindings,
  PHASE_CHECKS,
  type PhaseKey,
  phaseCompleted,
  SEVERITY_LABEL,
  type Severity,
  sortFindings,
  stepFailed,
} from '@/lib/workflowFindings'
import { type ReportKey, reportFile, WorkflowReports } from './WorkflowReports'

const PHASES: { key: PhaseKey; title: string; when: string; duty: string }[] = [
  { key: 'open-day', title: 'Open', when: '09:12', duty: 'Is the stack ready to trade' },
  {
    key: 'liveness',
    title: 'After the open',
    when: '09:27',
    duty: 'Is the engine actually trading',
  },
  {
    key: 'close-day',
    title: 'Close',
    when: '15:36',
    duty: 'Books, data, errors and jobs, then the review',
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

const SEVERITY_TONE: Record<Severity, Tone> = {
  critical: 'bad',
  warning: 'warn',
  unverified: 'idle',
  info: 'info',
}

const HEALTH: Record<Health, { tone: Tone; label: string }> = {
  healthy: { tone: 'good', label: 'Healthy' },
  degraded: { tone: 'warn', label: 'Degraded' },
  critical: { tone: 'bad', label: 'Critical' },
  nodata: { tone: 'idle', label: 'No data' },
}

function seconds(hms: string): number {
  const [h, m, s] = hms.split(':').map(Number)
  return h * 3600 + m * 60 + s
}

function duration(step: WorkflowStep): string {
  const total = Math.max(0, seconds(step.ended) - seconds(step.started))
  return total >= 60 ? `${Math.floor(total / 60)}m ${total % 60}s` : `${total}s`
}

function phaseFindings(findings: Finding[], phase: PhaseKey): Finding[] {
  return findings.filter((f) => f.phase === phase)
}

function summaryLine(counts: Record<Severity, number>): string {
  const parts = [
    counts.critical && `${counts.critical} critical`,
    counts.warning && `${counts.warning} ${counts.warning === 1 ? 'warning' : 'warnings'}`,
    counts.unverified && `${counts.unverified} unverified`,
  ].filter(Boolean)
  return parts.length ? parts.join(', ') : 'No open findings'
}

function Stat({
  label,
  value,
  tone,
  sub,
  to,
}: {
  label: string
  value: string | number
  tone: Tone
  sub?: string
  to?: string
}) {
  const body = (
    <div className={`h-full rounded-md border px-3 py-2 ${to ? 'hover:bg-muted/50' : ''}`}>
      <div className="text-[13px] text-muted-foreground">{label}</div>
      <div className={`text-2xl font-semibold tabular-nums ${TONE_TEXT[tone]}`}>{value}</div>
      {sub && <div className="text-[13px] text-muted-foreground">{sub}</div>}
    </div>
  )
  return to ? <Link to={to}>{body}</Link> : body
}

function SessionOverview({
  day,
  findings,
  bugs,
}: {
  day: WorkflowDay
  findings: Finding[]
  bugs: BugSummary | null
}) {
  const counts = countSeverities(findings)
  const h = HEALTH[healthOf(day, findings)]
  const done = PHASES.filter((p) => phaseCompleted(day, p.key) === true).length
  const withFailedSteps = PHASES.filter((p) => phaseCompleted(day, p.key) === false).length
  const ran = done + withFailedSteps
  return (
    <Card>
      <CardContent className="pt-6 space-y-4">
        <div className="flex flex-col lg:flex-row gap-6">
          <div className="flex items-start gap-3 lg:w-2/5">
            <ToneIcon tone={h.tone} className="h-10 w-10 shrink-0" />
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span className="text-xl font-bold">{h.label}</span>
                <span className="text-[15px] text-muted-foreground">{day.day}</span>
              </div>
              <p className="mt-1 text-[15px]">{summaryLine(counts)}</p>
              <p className="mt-1 text-[15px] text-muted-foreground">
                {day.verdict
                  ? `Close-day review (${day.verdict.verdict}): ${day.verdict.line}`
                  : 'No close-day review verdict yet. It is written after the 15:36 close checks.'}
              </p>
            </div>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-6 gap-2 lg:flex-1">
            <Stat
              label="Checkpoints completed"
              value={`${done} / ${PHASES.length}`}
              tone={withFailedSteps ? 'warn' : ran === 0 ? 'idle' : 'info'}
              sub={
                withFailedSteps
                  ? `${withFailedSteps} had failed steps`
                  : ran < PHASES.length
                    ? `${PHASES.length - ran} not run`
                    : 'all steps exited cleanly'
              }
            />
            <Stat
              label="Failed checks"
              value={counts.critical}
              tone={counts.critical ? 'bad' : 'idle'}
            />
            <Stat label="Warnings" value={counts.warning} tone={counts.warning ? 'warn' : 'idle'} />
            <Stat
              label="Unverified"
              value={counts.unverified}
              tone="idle"
              sub={counts.unverified ? 'not confirmed healthy' : undefined}
            />
            <Stat
              label="Bugs needing a fix"
              value={bugs ? bugs.needsFix : 'n/a'}
              tone={bugs?.highNeedsFix ? 'bad' : bugs?.needsFix ? 'warn' : 'idle'}
              sub={bugs ? `${bugs.highNeedsFix} high risk` : 'tracker unavailable'}
              to="/logs/bugs"
            />
            <Stat
              label="High-risk awaiting proof"
              value={bugs ? bugs.highAwaitingProof : 'n/a'}
              tone={bugs?.highAwaitingProof ? 'warn' : 'idle'}
              sub={bugs ? `${bugs.awaitingProof} awaiting proof in all` : 'tracker unavailable'}
              to="/logs/bugs?group=proving&severity=HIGH"
            />
          </div>
        </div>
        <p className="text-[13px] text-muted-foreground border-t pt-3">
          Status comes from what the checks found. A checkpoint can complete cleanly and still
          report failures, and an unverified check never counts as healthy.
        </p>
      </CardContent>
    </Card>
  )
}

/** The last few days as one cell each, so a bad day stands out without paging back. */
function Trend({
  days,
  current,
  onSelect,
}: {
  days: string[]
  current: string
  onSelect: (d: string) => void
}) {
  const recent = days.slice(0, 7)
  const results = useQueries({
    queries: recent.map((d) => ({
      queryKey: ['ops-workflow', d],
      queryFn: () => opsWorkflowApi.get(d),
      staleTime: 5 * 60_000,
    })),
  })
  if (recent.length < 2) return null
  return (
    <div className="flex items-center gap-2 flex-wrap">
      <span className="text-[13px] font-semibold uppercase text-muted-foreground">Last days</span>
      {[...recent].reverse().map((d) => {
        const view = results[recent.indexOf(d)]?.data?.data
        const health = view ? HEALTH[healthOf(view, buildFindings(view))] : null
        return (
          <button
            type="button"
            key={d}
            onClick={() => onSelect(d)}
            title={`${d}: ${health?.label ?? 'No result'}${view?.verdict ? ` - ${view.verdict.line}` : ''}`}
            className={`flex items-center gap-1.5 rounded-md border px-2 py-1 text-[13px] transition-colors ${
              d === current
                ? 'border-primary ring-1 ring-primary bg-primary/5'
                : 'hover:bg-muted/50'
            }`}
          >
            <ToneIcon tone={health?.tone ?? 'idle'} className="h-3 w-3" />
            {d.slice(5)}
            <span className="text-muted-foreground">{health?.label ?? 'No result'}</span>
          </button>
        )
      })}
    </div>
  )
}

/** The day on screen and the bugs already filed for its findings, keyed by finding reference. */
const FindingBugs = createContext<{ day: string; links: Map<string, FindingLink> }>({
  day: '',
  links: new Map(),
})

function FindingRow({
  finding,
  onOpenReport,
}: {
  finding: Finding
  onOpenReport: (key: ReportKey) => void
}) {
  const [open, setOpen] = useState(false)
  const [reporting, setReporting] = useState(false)
  const { day, links } = useContext(FindingBugs)
  const bug = links.get(findingRef(finding))
  const phase = PHASES.find((p) => p.key === finding.phase)
  return (
    <li className="px-4 py-2.5">
      <div className="flex flex-wrap items-start gap-x-3 gap-y-1">
        <StatusBadge tone={SEVERITY_TONE[finding.severity]}>
          {SEVERITY_LABEL[finding.severity]}
        </StatusBadge>
        <div className="min-w-[14rem] flex-1">
          <div className="text-[15px] break-words">{finding.title}</div>
          <div className="text-[13px] text-muted-foreground">
            {finding.source} | {phase?.title} checkpoint
            {finding.checkedAt ? `, ${finding.checkedAt.slice(0, 5)}` : ''} |{' '}
            <span className={finding.severity === 'critical' ? TONE_TEXT.bad : ''}>
              {ACTION_TEXT[finding.severity]}
            </span>
          </div>
        </div>
        <div className="flex items-center gap-3 text-[13px]">
          {finding.report && (
            <button
              type="button"
              className="text-primary hover:underline"
              onClick={() => onOpenReport(finding.report as ReportKey)}
            >
              Open report
            </button>
          )}
          {bug ? (
            <Link
              to={`/logs/bugs?q=${bug.bug_id}&group=${groupOfStatus(bug.status)}`}
              className="font-medium text-primary hover:underline"
              title="Open this bug in the Bug Tracker"
            >
              Tracked as {bug.bug_id}
            </Link>
          ) : (
            day && (
              <button
                type="button"
                className="text-primary hover:underline"
                onClick={() => setReporting(true)}
              >
                Report as bug
              </button>
            )
          )}
          <button
            type="button"
            aria-expanded={open}
            className="flex items-center gap-0.5 text-muted-foreground hover:text-foreground"
            onClick={() => setOpen(!open)}
          >
            <ChevronDown className={`h-3 w-3 transition-transform ${open ? 'rotate-180' : ''}`} />
            Details
          </button>
        </div>
      </div>
      {open && (
        <pre className="mt-2 whitespace-pre-wrap break-words rounded-md bg-muted/60 p-2 text-[13px] font-mono">
          {finding.evidence}
        </pre>
      )}
      {reporting && (
        <ReportBugDialog finding={finding} day={day} onClose={() => setReporting(false)} />
      )}
    </li>
  )
}

function Attention({
  findings,
  hasData,
  onOpenReport,
}: {
  findings: Finding[]
  hasData: boolean
  onOpenReport: (key: ReportKey) => void
}) {
  const [showInfo, setShowInfo] = useState(false)
  const actionable = findings.filter((f) => f.severity !== 'info')
  const informational = findings.filter((f) => f.severity === 'info')
  const groups = CATEGORIES.map((c) => ({
    ...c,
    items: sortFindings(actionable.filter((f) => f.category === c.key)),
  })).filter((g) => g.items.length > 0)
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">
          Needs attention{' '}
          <span className="font-normal text-muted-foreground">({actionable.length})</span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {!hasData ? (
          <p className="text-[15px] text-muted-foreground">
            No checkpoint has run on this day, so there is nothing to assess.
          </p>
        ) : groups.length === 0 ? (
          <p className="text-[15px]">
            <StatusBadge tone="good">Clear</StatusBadge>
            <span className="ml-2">
              Nothing failed, warned or went unverified in the recorded checks.
            </span>
          </p>
        ) : (
          groups.map((g) => (
            <div key={g.key} className="rounded-lg border">
              <div className="flex items-center justify-between border-b bg-muted/30 px-4 py-2">
                <span className="text-[15px] font-medium">{g.label}</span>
                <span className="text-[13px] text-muted-foreground">{g.items.length}</span>
              </div>
              <ul className="divide-y">
                {g.items.map((f) => (
                  <FindingRow key={f.id} finding={f} onOpenReport={onOpenReport} />
                ))}
              </ul>
            </div>
          ))
        )}
        {informational.length > 0 && (
          <div>
            <button
              type="button"
              className="text-[13px] text-muted-foreground hover:text-foreground"
              onClick={() => setShowInfo(!showInfo)}
            >
              {showInfo ? 'Hide' : 'Show'} {informational.length} informational{' '}
              {informational.length === 1 ? 'item' : 'items'} (on watch, no action)
            </button>
            {showInfo && (
              <ul className="mt-2 divide-y rounded-lg border">
                {informational.map((f) => (
                  <FindingRow key={f.id} finding={f} onOpenReport={onOpenReport} />
                ))}
              </ul>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function Timeline({
  day,
  findings,
  selected,
  onSelect,
}: {
  day: WorkflowDay
  findings: Finding[]
  selected: PhaseKey
  onSelect: (p: PhaseKey) => void
}) {
  return (
    <div className="relative">
      <div
        className="absolute left-0 right-0 top-5 hidden border-t border-dashed sm:block"
        aria-hidden="true"
      />
      <ol className="relative grid grid-cols-1 sm:grid-cols-3 gap-3">
        {PHASES.map((p) => {
          const steps = day.phases.find((x) => x.phase === p.key)?.steps ?? null
          const counts = countSeverities(phaseFindings(findings, p.key))
          const completed = phaseCompleted(day, p.key)
          const needs = counts.critical + counts.warning + counts.unverified > 0
          const tone: Tone =
            completed === null
              ? 'idle'
              : counts.critical
                ? 'bad'
                : counts.warning
                  ? 'warn'
                  : counts.unverified
                    ? 'idle'
                    : 'good'
          const status =
            completed === null ? 'Not run' : completed ? 'Completed' : 'Completed with failed steps'
          const active = selected === p.key
          return (
            <li key={p.key}>
              <button
                type="button"
                aria-current={active}
                onClick={() => onSelect(p.key)}
                className={`w-full text-left rounded-lg border bg-card p-3 transition-colors ${
                  active ? 'border-primary ring-1 ring-primary' : 'hover:bg-muted/50'
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="flex items-center gap-2 font-semibold">
                    <ToneIcon tone={tone} />
                    {p.title}
                  </span>
                  {needs && (
                    <StatusBadge tone={counts.critical ? 'bad' : 'warn'}>
                      Needs attention
                    </StatusBadge>
                  )}
                </div>
                <div className="mt-1 flex items-center gap-1 text-[13px] text-muted-foreground">
                  <Clock className="h-3 w-3" />
                  Scheduled {p.when}
                  {steps?.length ? `, ran ${steps[0].started}` : ''}
                </div>
                <div className="mt-2 text-[15px]">{status}</div>
                <div className="mt-1 flex flex-wrap gap-x-3 text-[13px] text-muted-foreground">
                  {completed === null ? (
                    <span>No checks recorded</span>
                  ) : (
                    <>
                      <span className={counts.critical ? TONE_TEXT.bad : ''}>
                        {counts.critical} failed
                      </span>
                      <span className={counts.warning ? TONE_TEXT.warn : ''}>
                        {counts.warning} {counts.warning === 1 ? 'warning' : 'warnings'}
                      </span>
                      <span>{counts.unverified} unverified</span>
                    </>
                  )}
                </div>
              </button>
            </li>
          )
        })}
      </ol>
    </div>
  )
}

function Steps({
  steps,
  day,
  phase,
}: {
  steps: WorkflowStep[] | null | undefined
  day: WorkflowDay
  phase: PhaseKey
}) {
  if (!steps) {
    return (
      <p className="text-[15px] text-muted-foreground">This checkpoint did not run on this day.</p>
    )
  }
  return (
    <ol className="space-y-2">
      {steps.map((s) => (
        <li key={s.step} className="flex items-start gap-3 rounded-md border p-2">
          <ToneIcon
            tone={s.exit === 0 ? 'good' : stepFailed(day, phase, s) ? 'bad' : 'warn'}
            className="h-4 w-4 mt-0.5 shrink-0"
          />
          <div className="flex-1 min-w-0">
            <div className="flex justify-between gap-2">
              <span className="text-[15px] font-medium">{STEP_INFO[s.step] ?? s.step}</span>
              <span className="text-[13px] text-muted-foreground whitespace-nowrap">
                {s.started} | {duration(s)}
              </span>
            </div>
            {s.exit !== 0 &&
              (stepFailed(day, phase, s) ? (
                <div className={`text-[13px] ${TONE_TEXT.bad}`}>
                  {s.exit === 124 ? 'Stopped at the deadline' : `Failed (exit ${s.exit})`}
                </div>
              ) : (
                <div className={`text-[13px] ${TONE_TEXT.warn}`}>
                  Ran and reported RED. See the findings.
                </div>
              ))}
          </div>
        </li>
      ))}
    </ol>
  )
}

function PassedChecks({ day, phase }: { day: WorkflowDay; phase: PhaseKey }) {
  const [open, setOpen] = useState(false)
  const groups = PHASE_CHECKS[phase]
    .filter((k) => day.checks[k])
    .map((k) => ({ key: k, ok: day.checks[k].checks.filter((c) => c.status === 'OK') }))
  const total = groups.reduce((n, g) => n + g.ok.length, 0)
  if (total === 0) return null
  return (
    <div>
      <button
        type="button"
        className="text-[13px] text-muted-foreground hover:text-foreground"
        onClick={() => setOpen(!open)}
      >
        {open ? 'Hide' : 'Show'} {total} checks that passed
      </button>
      {open && (
        <div className="mt-2 space-y-3">
          {groups.map((g) => (
            <div key={g.key}>
              <div className="text-[13px] font-medium">{CHECK_TITLES[g.key] ?? g.key}</div>
              <ul className="mt-1 space-y-1">
                {g.ok.map((c) => (
                  <li key={`${c.name}-${c.evidence}`} className="text-[13px] text-muted-foreground">
                    <span className="text-foreground">{c.plain || c.name}</span> ({c.evidence})
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function CheckpointDetails({
  day,
  phase,
  findings,
  onOpenReport,
}: {
  day: WorkflowDay
  phase: PhaseKey
  findings: Finding[]
  onOpenReport: (key: ReportKey) => void
}) {
  const meta = PHASES.find((p) => p.key === phase) ?? PHASES[2]
  const list = sortFindings(phaseFindings(findings, phase))
  const results = PHASE_CHECKS[phase].filter((k) => day.checks[k])
  const steps = day.phases.find((p) => p.phase === phase)?.steps
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">
          {meta.title}{' '}
          <span className="font-normal text-muted-foreground">
            {meta.when} - {meta.duty}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="grid grid-cols-1 lg:grid-cols-5 gap-6">
        <div className="lg:col-span-2 space-y-4">
          <div>
            <div className="text-[13px] font-semibold uppercase text-muted-foreground mb-2">
              Steps executed
            </div>
            <Steps steps={steps} day={day} phase={phase} />
          </div>
          {results.length > 0 && (
            <div>
              <div className="text-[13px] font-semibold uppercase text-muted-foreground mb-2">
                Check results
              </div>
              <ul className="space-y-1.5">
                {results.map((k) => {
                  const r = day.checks[k]
                  const bad = r.checks.filter((c) => c.status !== 'OK').length
                  const tone: Tone =
                    r.verdict === 'GREEN' ? 'good' : r.verdict === 'RED' ? 'bad' : 'warn'
                  return (
                    <li
                      key={k}
                      className="flex items-center justify-between gap-2 rounded-md border px-2 py-1.5"
                    >
                      <div className="min-w-0">
                        <div className="text-[15px] font-medium">{CHECK_TITLES[k] ?? k}</div>
                        <div className="text-[13px] text-muted-foreground">
                          {r.checks.length - bad} of {r.checks.length} passed
                        </div>
                      </div>
                      <StatusBadge tone={tone}>{r.verdict}</StatusBadge>
                    </li>
                  )
                })}
              </ul>
            </div>
          )}
        </div>
        <div className="lg:col-span-3 space-y-3">
          <div className="text-[13px] font-semibold uppercase text-muted-foreground">
            Findings and observations
          </div>
          {list.length === 0 ? (
            <p className="text-[15px] text-muted-foreground">
              {steps
                ? 'No failures, warnings or unverified checks were recorded for this checkpoint.'
                : 'No findings. This checkpoint did not run.'}
            </p>
          ) : (
            <ul className="divide-y rounded-lg border">
              {list.map((f) => (
                <FindingRow key={f.id} finding={f} onOpenReport={onOpenReport} />
              ))}
            </ul>
          )}
          <PassedChecks day={day} phase={phase} />
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
  const [params] = useSearchParams()
  const dayParam = params.get('day')
  const [day, setDay] = useState<string | undefined>(
    dayParam && /^\d{4}-\d{2}-\d{2}$/.test(dayParam) ? dayParam : undefined
  )
  const [phase, setPhase] = useState<PhaseKey | null>(null)
  const [report, setReport] = useState<ReportKey | null>(null)
  const reportsRef = useRef<HTMLDivElement>(null)

  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ['ops-workflow', day],
    queryFn: () => opsWorkflowApi.get(day),
    // Today's phases land through the day; a past day never changes.
    refetchInterval: day === undefined ? 60_000 : false,
  })
  const view = data?.data
  const days = data?.days ?? []
  const index = view ? days.indexOf(view.day) : -1

  // The execution-health table feeds extra findings; the report panel shares this cached request.
  const execFile = view ? reportFile(view.reports, 'execution') : undefined
  const exec = useQuery({
    queryKey: ['ops-report', execFile],
    queryFn: () => opsWorkflowApi.report(execFile ?? ''),
    enabled: execFile !== undefined,
  })
  const bugs = useQuery({
    queryKey: ['bug-tracker'],
    queryFn: bugsApi.list,
    refetchInterval: 60_000,
  })
  const linkRows = useQuery({
    queryKey: ['finding-links', view?.day],
    queryFn: () => opsFindingsApi.links(view?.day ?? ''),
    enabled: Boolean(view?.day),
  })
  const linkMap = useMemo(
    () => new Map((linkRows.data ?? []).map((l) => [findingRef(l), l])),
    [linkRows.data]
  )
  const bugContext = useMemo(() => ({ day: view?.day ?? '', links: linkMap }), [view?.day, linkMap])
  const bugSummary = useMemo(() => (bugs.data ? summarizeBugs(bugs.data) : null), [bugs.data])

  const findings = useMemo(() => {
    if (!view) return []
    const metrics = exec.data ? (parseExecutionHealth(exec.data)?.rows ?? []) : []
    return [...buildFindings(view), ...metricFindings(metrics, view)]
  }, [view, exec.data])

  const selected = phase ?? defaultPhase(findings) ?? (view ? lastRunPhase(view) : 'close-day')
  const pick = (d: string) => {
    setDay(d)
    setPhase(null)
    setReport(null)
  }
  const openReport = (key: ReportKey) => {
    setReport(key)
    reportsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
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
            Is the trading system healthy, what happened, and what needs your attention
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
          <CardContent className="pt-6 text-[15px]">{(error as Error).message}</CardContent>
        </Card>
      ) : !view || !view.day ? (
        <Card>
          <CardContent className="pt-6 text-[15px] text-muted-foreground">
            No workflow runs yet. The first runs at 09:12 on the next trading day.
          </CardContent>
        </Card>
      ) : (
        <FindingBugs.Provider value={bugContext}>
          <SessionOverview day={view} findings={findings} bugs={bugSummary} />
          <Trend days={days} current={view.day} onSelect={pick} />
          <Attention
            findings={findings}
            hasData={healthOf(view, findings) !== 'nodata'}
            onOpenReport={openReport}
          />
          <section aria-label="Session timeline" className="space-y-2">
            <div className="text-[13px] font-semibold uppercase text-muted-foreground">
              Session timeline
            </div>
            <Timeline day={view} findings={findings} selected={selected} onSelect={setPhase} />
          </section>
          <CheckpointDetails
            day={view}
            phase={selected}
            findings={findings}
            onOpenReport={openReport}
          />
          <div ref={reportsRef} className="scroll-mt-4">
            <WorkflowReports key={view.day} day={view} selected={report} onSelect={setReport} />
          </div>
        </FindingBugs.Provider>
      )}
    </div>
  )
}
