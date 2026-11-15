import { useQueries, useQuery } from '@tanstack/react-query'
import {
  CalendarCheck,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  FileText,
  RefreshCw,
} from 'lucide-react'
import { createContext, useContext, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { bugsApi } from '@/api/bugs'
import { type FindingLink, opsFindingsApi } from '@/api/opsFindings'
import { opsWorkflowApi, type WorkflowDay, type WorkflowStep } from '@/api/opsWorkflow'
import { ReportBugDialog } from '@/components/ops/ReportBugDialog'
import { RemedyProvider, RunAgainButton, RunBanner } from '@/components/ops/RunAgain'
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
import { dayBars, formatDuration, stepBars } from '@/lib/workflowCharts'
import {
  ACTION_TEXT,
  buildFindings,
  CATEGORIES,
  countSeverities,
  type Finding,
  type Health,
  metricFindings,
  PHASE_CHECKS,
  type PhaseKey,
  phaseCompleted,
  SEVERITY_LABEL,
  type Severity,
  sortFindings,
  stepFailed,
} from '@/lib/workflowFindings'
import { lastUpdatedText, nextDayParam, parseDayParam, pollInterval } from '@/lib/workflowNav'
import {
  combinedHealthOf,
  noticeLines,
  noticeNeedsLook,
  phaseTime,
  type RunTone,
  recurrenceOf,
  recurrenceText,
  recurringFailures,
  reviewStatusText,
  runFor,
  runLabel,
  summarizeRuns,
} from '@/lib/workflowStatus'
import { type ReportKey, reportFile, WorkflowReports } from './WorkflowReports'

const CurrentDay = createContext<WorkflowDay | null>(null)

const PHASES: { key: PhaseKey; title: string; duty: string }[] = [
  { key: 'open-day', title: 'Open', duty: 'Is the stack ready to trade' },
  {
    key: 'liveness',
    title: 'After the open',
    duty: 'Is the engine actually trading',
  },
  {
    key: 'close-day',
    title: 'Close',
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
  const h = HEALTH[combinedHealthOf(day, findings)]
  return (
    <Card>
      <CardContent className="pt-6">
        <div className="flex items-start gap-3">
          <ToneIcon tone={h.tone} className="h-10 w-10 shrink-0" />
          <div className="min-w-0 space-y-1">
            <div className="flex flex-wrap items-baseline gap-x-2">
              <span className="text-xl font-bold">{h.label}</span>
              <span className="text-[15px] text-muted-foreground">{day.day}</span>
            </div>
            <p className="text-[15px]">{summaryLine(counts)}</p>
            <p className="text-[15px] text-muted-foreground">{reviewStatusText(day)}</p>
            <p className="text-[15px]">
              {bugs ? (
                <>
                  <Link
                    to="/logs/bugs"
                    className={`font-medium hover:underline ${
                      bugs.highNeedsFix ? TONE_TEXT.bad : 'text-primary'
                    }`}
                  >
                    {bugs.needsFix} {bugs.needsFix === 1 ? 'bug needs' : 'bugs need'} a fix
                    {bugs.highNeedsFix ? ` (${bugs.highNeedsFix} high risk)` : ''}
                  </Link>
                  <span className="text-muted-foreground"> | </span>
                  <Link
                    to="/logs/bugs?group=proving"
                    className="text-primary hover:underline"
                    title="Fixed, but a live session still has to prove it"
                  >
                    {bugs.awaitingProof} awaiting proof
                    {bugs.highAwaitingProof ? ` (${bugs.highAwaitingProof} high risk)` : ''}
                  </Link>
                </>
              ) : (
                <span className="text-muted-foreground">Bug tracker unavailable</span>
              )}
            </p>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

/** The last days as stacked bars (failed on the bottom, warnings above), so a bad day stands out. */
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
  const bars = dayBars(
    recent.map((d, i) => {
      const view = results[i]?.data?.data
      const counts = view ? countSeverities(buildFindings(view)) : null
      return {
        day: d,
        critical: counts?.critical ?? 0,
        warning: counts?.warning ?? 0,
        hasData: Boolean(view),
      }
    })
  )
  return (
    <Card>
      <CardContent className="pt-4 pb-3">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <span className="text-[13px] font-semibold uppercase text-muted-foreground">
            Last {bars.length} days
          </span>
          <span className="flex items-center gap-3 text-[13px] text-muted-foreground">
            <span className="flex items-center gap-1">
              <span className="inline-block h-2.5 w-2.5 rounded-sm bg-red-600" />
              failed
            </span>
            <span className="flex items-center gap-1">
              <span className="inline-block h-2.5 w-2.5 rounded-sm bg-amber-500" />
              warnings
            </span>
          </span>
        </div>
        <div className="flex items-end gap-2">
          {bars.map((b) => (
            <button
              type="button"
              key={b.day}
              onClick={() => onSelect(b.day)}
              title={`${b.day}: ${b.hasData ? `${b.critical} failed, ${b.warning} warnings` : 'loading'}`}
              className={`flex min-w-0 flex-1 flex-col items-center gap-1 rounded-md border px-1 pb-1 pt-2 transition-colors ${
                b.day === current
                  ? 'border-primary ring-1 ring-primary bg-primary/5'
                  : 'hover:bg-muted/50'
              }`}
            >
              <span className="flex h-14 w-full max-w-10 flex-col justify-end overflow-hidden rounded-sm bg-muted/40">
                <span className="block bg-amber-500" style={{ height: `${b.warningPct}%` }} />
                <span className="block bg-red-600" style={{ height: `${b.criticalPct}%` }} />
              </span>
              <span className="text-[13px] tabular-nums">{b.day.slice(5)}</span>
              <span
                className={`text-[13px] tabular-nums ${b.critical ? TONE_TEXT.bad : 'text-muted-foreground'}`}
              >
                {b.hasData ? (b.critical ? `${b.critical} failed` : 'clear') : '...'}
              </span>
            </button>
          ))}
        </div>
      </CardContent>
    </Card>
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
  const workflowDay = useContext(CurrentDay)
  const repeat =
    workflowDay && finding.severity === 'critical'
      ? recurrenceOf(workflowDay, finding.group, finding.name)
      : undefined
  const bug = links.get(findingRef(finding))
  const phase = PHASES.find((p) => p.key === finding.phase)
  const category = CATEGORIES.find((c) => c.key === finding.category)?.label
  return (
    <li className="px-4 py-2.5">
      <div className="flex flex-wrap items-start gap-x-3 gap-y-1">
        <StatusBadge tone={SEVERITY_TONE[finding.severity]}>
          {SEVERITY_LABEL[finding.severity]}
        </StatusBadge>
        <div className="min-w-[14rem] flex-1">
          <div className="text-[15px] break-words">{finding.title}</div>
          <div className="text-[13px] text-muted-foreground">
            {[category, phase?.title, finding.checkedAt?.slice(0, 5)].filter(Boolean).join(' | ')}
            {' | '}
            <span className={finding.severity === 'critical' ? TONE_TEXT.bad : ''}>
              {ACTION_TEXT[finding.severity]}
            </span>
          </div>
          {repeat && workflowDay?.recurrence && (
            <div
              className={`text-[13px] ${repeat.count >= 2 ? TONE_TEXT.warn : 'text-muted-foreground'}`}
            >
              {repeat.count >= 2 ? 'Recurring: ' : ''}
              {recurrenceText(repeat, workflowDay.recurrence.window)}
            </div>
          )}
        </div>
        <div className="flex items-center gap-3 text-[13px]">
          {bug && (
            <Link
              to={`/logs/bugs?q=${bug.bug_id}&group=${groupOfStatus(bug.status)}`}
              className="font-medium text-primary hover:underline"
              title="Open this bug in the Bug Tracker"
            >
              Tracked as {bug.bug_id}
            </Link>
          )}
          <RunAgainButton finding={finding} />
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
        <div className="mt-2 space-y-2">
          <pre className="whitespace-pre-wrap break-words rounded-md bg-muted/60 p-2 text-[13px] font-mono">
            {finding.evidence}
          </pre>
          <div className="flex items-center gap-4 text-[13px]">
            {finding.report && (
              <button
                type="button"
                className="text-primary hover:underline"
                onClick={() => onOpenReport(finding.report as ReportKey)}
              >
                Open report
              </button>
            )}
            {!bug && day && (
              <button
                type="button"
                className="text-primary hover:underline"
                onClick={() => setReporting(true)}
              >
                Report as bug
              </button>
            )}
          </div>
        </div>
      )}
      {reporting && (
        <ReportBugDialog finding={finding} day={day} onClose={() => setReporting(false)} />
      )}
    </li>
  )
}

const RUN_TONE: Record<RunTone, Tone> = { good: 'good', warn: 'warn', bad: 'bad', idle: 'idle' }

/** Expected versus completed runs: one line, then the phases that did not run cleanly. */
function RunsCard({ day }: { day: WorkflowDay }) {
  const summary = summarizeRuns(day.run_status)
  if (!summary) return null
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">Expected runs</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <p className="text-[15px]">{summary.text}</p>
        <ul className="grid gap-2 sm:grid-cols-3">
          {(day.run_status ?? []).map((r) => {
            const l = runLabel(r)
            const title = PHASES.find((p) => p.key === r.phase)
            return (
              <li key={r.phase} className="rounded-md border px-3 py-2">
                <div className="text-[13px] text-muted-foreground">
                  {title?.title} | due {r.due}
                  {r.started_at ? `, started ${r.started_at.slice(11, 16)}` : ''}
                </div>
                {l && <StatusBadge tone={RUN_TONE[l.tone]}>{l.label}</StatusBadge>}
                {r.note && <div className="mt-1 text-[13px] text-muted-foreground">{r.note}</div>}
              </li>
            )
          })}
        </ul>
      </CardContent>
    </Card>
  )
}

/** Checks that failed on 2 or more of the last sessions. Hidden when there are none. */
function RecurringCard({ day }: { day: WorkflowDay }) {
  const items = recurringFailures(day)
  if (!day.recurrence) return null
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">
          Recurring failures{' '}
          <span className="font-normal text-muted-foreground">
            (last {day.recurrence.window} sessions)
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent>
        {items.length === 0 ? (
          <p className="text-[15px] text-muted-foreground">No check failed more than once.</p>
        ) : (
          <ul className="divide-y rounded-lg border">
            {items.map((i) => (
              <li key={`${i.group}|${i.name}`} className="px-4 py-2 text-[15px]">
                <span className="break-words">{i.name}</span>
                <div className="text-[13px] text-muted-foreground">
                  {recurrenceText(i, day.recurrence?.window ?? 0)}
                  {i.failing_today ? ' | failing on this day' : ''}
                </div>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}

/** Last alert sent and last recovery, from the alert state file. No message text is shown. */
function NoticeCard({ day }: { day: WorkflowDay }) {
  const lines = noticeLines(day.notifications)
  if (!lines) return null
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">Last alert and recovery</CardTitle>
      </CardHeader>
      <CardContent>
        <dl className="grid gap-x-4 gap-y-1 text-[15px] sm:grid-cols-[12rem_1fr]">
          {lines.map((l) => (
            <div key={l.label} className="contents">
              <dt className="text-muted-foreground">{l.label}</dt>
              <dd
                className={`break-words ${
                  l.label === 'Open red alerts' && noticeNeedsLook(day.notifications)
                    ? TONE_TEXT.bad
                    : ''
                }`}
              >
                {l.value}
              </dd>
            </div>
          ))}
        </dl>
      </CardContent>
    </Card>
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
  const [showMore, setShowMore] = useState(false)
  const [showInfo, setShowInfo] = useState(false)
  const actionable = sortFindings(findings.filter((f) => f.severity !== 'info'))
  const critical = actionable.filter((f) => f.severity === 'critical')
  const lesser = actionable.filter((f) => f.severity !== 'critical')
  const informational = sortFindings(findings.filter((f) => f.severity === 'info'))
  // Failures come first and stay in view. Warnings and unverified items wait one click away,
  // unless nothing failed, when they are the whole story.
  const lesserOpen = showMore || critical.length === 0
  const list = (items: Finding[]) => (
    <ul className="divide-y rounded-lg border">
      {items.map((f) => (
        <FindingRow key={f.id} finding={f} onOpenReport={onOpenReport} />
      ))}
    </ul>
  )
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
        ) : actionable.length === 0 ? (
          <p className="text-[15px]">
            <StatusBadge tone="good">Clear</StatusBadge>
            <span className="ml-2">Nothing failed, warned or went unverified.</span>
          </p>
        ) : (
          <>
            {critical.length > 0 && list(critical)}
            {lesser.length > 0 && (
              <div>
                {critical.length > 0 && (
                  <button
                    type="button"
                    aria-expanded={showMore}
                    className="mb-2 text-[13px] text-muted-foreground hover:text-foreground"
                    onClick={() => setShowMore(!showMore)}
                  >
                    {showMore ? 'Hide' : 'Show'} {lesser.length}{' '}
                    {lesser.length === 1
                      ? 'warning or unverified item'
                      : 'warnings and unverified items'}
                  </button>
                )}
                {lesserOpen && list(lesser)}
              </div>
            )}
          </>
        )}
        {informational.length > 0 && (
          <div>
            <button
              type="button"
              className="text-[13px] text-muted-foreground hover:text-foreground"
              onClick={() => setShowInfo(!showInfo)}
            >
              {showInfo ? 'Hide' : 'Show'} {informational.length} on watch (no action)
            </button>
            {showInfo && <div className="mt-2">{list(informational)}</div>}
          </div>
        )}
      </CardContent>
    </Card>
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
  const bars = stepBars(steps)
  return (
    <ol className="space-y-2">
      {steps.map((s, i) => {
        const failed = s.exit !== 0 && stepFailed(day, phase, s)
        const tone: Tone = s.exit === 0 ? 'good' : failed ? 'bad' : 'warn'
        const fill = { good: 'bg-green-600', bad: 'bg-red-600', warn: 'bg-amber-500' }[
          tone as 'good' | 'bad' | 'warn'
        ]
        return (
          <li key={s.step} className="flex items-start gap-3 rounded-md border bg-card p-2">
            <ToneIcon tone={tone} className="h-4 w-4 mt-0.5 shrink-0" />
            <div className="flex-1 min-w-0">
              <div className="flex justify-between gap-2">
                <span className="text-[15px] font-medium">{STEP_INFO[s.step] ?? s.step}</span>
                <span className="text-[13px] text-muted-foreground whitespace-nowrap">
                  {s.reran ? 'run again, ' : ''}
                  {s.started.slice(0, 5)} | {formatDuration(bars[i].seconds)}
                </span>
              </div>
              <div className="mt-1 h-1.5 rounded-full bg-muted" aria-hidden="true">
                <div
                  className={`h-1.5 rounded-full ${fill}`}
                  style={{ width: `${bars[i].pct}%` }}
                />
              </div>
              {s.exit !== 0 &&
                (failed ? (
                  <div className={`text-[13px] ${TONE_TEXT.bad}`}>
                    {s.exit === 124 ? 'Stopped at the deadline' : `Failed (exit ${s.exit})`}
                  </div>
                ) : (
                  <div className={`text-[13px] ${TONE_TEXT.warn}`}>
                    {phase === 'close-day' && s.step === 'review' && day.review
                      ? `${day.review.text} (exit code ${s.exit} is diagnostic only)`
                      : 'Ran and reported RED. See the findings.'}
                  </div>
                ))}
            </div>
          </li>
        )
      })}
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

function Checkpoint({
  day,
  phase,
  findings,
  open,
  onToggle,
}: {
  day: WorkflowDay
  phase: (typeof PHASES)[number]
  findings: Finding[]
  open: boolean
  onToggle: () => void
}) {
  const steps = day.phases.find((p) => p.phase === phase.key)?.steps
  const counts = countSeverities(phaseFindings(findings, phase.key))
  const completed = phaseCompleted(day, phase.key)
  const run = runLabel(runFor(day, phase.key))
  const tone: Tone =
    completed === null
      ? run?.tone === 'bad'
        ? 'bad'
        : 'idle'
      : counts.critical
        ? 'bad'
        : counts.warning
          ? 'warn'
          : counts.unverified
            ? 'idle'
            : 'good'
  const results = PHASE_CHECKS[phase.key].filter((k) => day.checks[k])
  const status =
    completed === null
      ? (run?.label ?? 'Did not run')
      : [
          counts.critical && `${counts.critical} failed`,
          counts.warning && `${counts.warning} ${counts.warning === 1 ? 'warning' : 'warnings'}`,
          counts.unverified && `${counts.unverified} unverified`,
        ]
          .filter(Boolean)
          .join(', ') || 'All good'
  return (
    <li>
      <button
        type="button"
        aria-expanded={open}
        onClick={onToggle}
        className="flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-muted/50"
      >
        <ToneIcon tone={tone} className="h-5 w-5 shrink-0" />
        <span className="min-w-0 flex-1">
          <span className="font-medium">{phase.title}</span>
          <span className="ml-2 text-[13px] text-muted-foreground">
            {phaseTime(day, phase.key)}
            {steps?.length ? `, ran ${steps[0].started.slice(0, 5)}` : ''}
          </span>
          <span className="block text-[13px] text-muted-foreground">{phase.duty}</span>
        </span>
        <span className={`text-[15px] ${tone === 'bad' ? TONE_TEXT.bad : TONE_TEXT[tone]}`}>
          {status}
          {completed === false ? ' | a step failed' : ''}
          {completed !== null && run?.problem ? ` | ${run.label}` : ''}
        </span>
        <ChevronDown
          className={`h-4 w-4 shrink-0 text-muted-foreground transition-transform ${open ? 'rotate-180' : ''}`}
        />
      </button>
      {open && (
        <div className="grid grid-cols-1 gap-6 border-t bg-muted/20 px-4 py-4 lg:grid-cols-2">
          <div>
            <div className="mb-2 text-[13px] font-semibold uppercase text-muted-foreground">
              Steps
            </div>
            <Steps steps={steps} day={day} phase={phase.key} />
          </div>
          <div className="space-y-3">
            {results.length > 0 && (
              <div>
                <div className="mb-2 text-[13px] font-semibold uppercase text-muted-foreground">
                  Checks
                </div>
                <ul className="space-y-1.5">
                  {results.map((k) => {
                    const r = day.checks[k]
                    const bad = r.checks.filter((c) => c.status !== 'OK').length
                    const t: Tone =
                      r.verdict === 'GREEN' ? 'good' : r.verdict === 'RED' ? 'bad' : 'warn'
                    return (
                      <li
                        key={k}
                        className="flex items-center justify-between gap-2 rounded-md border bg-card px-2 py-1.5"
                      >
                        <span className="text-[15px]">
                          {CHECK_TITLES[k] ?? k}
                          <span className="ml-2 text-[13px] text-muted-foreground">
                            {r.checks.length - bad} of {r.checks.length} passed
                          </span>
                        </span>
                        <StatusBadge tone={t}>{r.verdict}</StatusBadge>
                      </li>
                    )
                  })}
                </ul>
              </div>
            )}
            <PassedChecks day={day} phase={phase.key} />
          </div>
        </div>
      )}
    </li>
  )
}

export default function DailyWorkflow() {
  // `?day=` is the source of truth for the selection (see lib/workflowNav.ts): the latest day is
  // never kept in the URL, so selecting it clears the parameter and live polling resumes.
  const [params, setParams] = useSearchParams()
  const day = parseDayParam(params.get('day'))
  const [openPhase, setOpenPhase] = useState<PhaseKey | null>(null)
  const [report, setReport] = useState<ReportKey | null>(null)
  const [showReports, setShowReports] = useState(false)
  const reportsRef = useRef<HTMLDivElement>(null)

  const { data, isLoading, error, refetch, isFetching, dataUpdatedAt } = useQuery({
    queryKey: ['ops-workflow', day],
    queryFn: () => opsWorkflowApi.get(day),
    // Today's phases land through the day. Poll while viewing the latest day (selected or not);
    // a past day is never polled, so it cannot change under the reader.
    refetchInterval: (query) => pollInterval(day, query.state.data?.days),
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

  const pick = (d: string) => {
    const next = nextDayParam(d, days)
    setParams(
      (prev) => {
        const out = new URLSearchParams(prev)
        if (next === null) out.delete('day')
        else out.set('day', next)
        return out
      },
      { replace: false }
    )
    setOpenPhase(null)
    setReport(null)
  }
  const lastUpdated = lastUpdatedText(dataUpdatedAt)
  const openReport = (key: ReportKey) => {
    setReport(key)
    setShowReports(true)
    // The panel mounts on this render; scroll once it exists.
    setTimeout(() => reportsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 0)
  }

  return (
    <div className="py-6 space-y-5">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <CalendarCheck className="h-6 w-6" />
            Daily Workflow
          </h1>
          <p className="text-muted-foreground mt-1">Is the system healthy, and what needs you</p>
        </div>
        <div className="flex min-w-0 max-w-full flex-wrap items-center gap-2">
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
          {lastUpdated && (
            <span
              className="whitespace-nowrap text-sm text-muted-foreground"
              title="Client time of the last successful fetch from the server"
              data-testid="last-updated"
            >
              {lastUpdated}
            </span>
          )}
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
            No workflow runs yet. The first runs at {phaseTime(view, 'open-day')} on the next
            trading day.
          </CardContent>
        </Card>
      ) : (
        <FindingBugs.Provider value={bugContext}>
          <RemedyProvider day={view.day}>
            <CurrentDay.Provider value={view}>
              <SessionOverview day={view} findings={findings} bugs={bugSummary} />
              <RunsCard day={view} />
              <Trend days={days} current={view.day} onSelect={pick} />
              <RunBanner />
              <Attention
                findings={findings}
                hasData={combinedHealthOf(view, findings) !== 'nodata'}
                onOpenReport={openReport}
              />
              <RecurringCard day={view} />
              <NoticeCard day={view} />
              <Card>
                <CardHeader className="pb-0">
                  <CardTitle className="text-base">Checkpoints</CardTitle>
                </CardHeader>
                <CardContent className="pt-3">
                  <ol className="divide-y rounded-lg border">
                    {PHASES.map((p) => (
                      <Checkpoint
                        key={p.key}
                        day={view}
                        phase={p}
                        findings={findings}
                        open={openPhase === p.key}
                        onToggle={() => setOpenPhase(openPhase === p.key ? null : p.key)}
                      />
                    ))}
                  </ol>
                </CardContent>
              </Card>
              <div ref={reportsRef} className="scroll-mt-4 space-y-3">
                <Button variant="outline" size="sm" onClick={() => setShowReports(!showReports)}>
                  <FileText className="h-4 w-4 mr-2" />
                  {showReports ? 'Hide full reports' : 'Show full reports'}
                </Button>
                {showReports && (
                  <WorkflowReports
                    key={view.day}
                    day={view}
                    selected={report}
                    onSelect={setReport}
                  />
                )}
              </div>
            </CurrentDay.Provider>
          </RemedyProvider>
        </FindingBugs.Provider>
      )}
    </div>
  )
}
