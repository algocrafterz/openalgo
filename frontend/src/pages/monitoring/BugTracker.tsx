import { useQuery } from '@tanstack/react-query'
import { Bug as BugIcon, ChevronDown, RefreshCw, Search, SlidersHorizontal, X } from 'lucide-react'
import { type ReactNode, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { type Bug, type BugKind, type BugSeverity, type BugStatus, bugsApi } from '@/api/bugs'
import { StatusBadge, TONE_STYLE, TONE_TEXT, type Tone } from '@/components/ops/StatusBadge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { seenPerDay, statusBreakdown } from '@/lib/bugCharts'
import {
  type BugFilters,
  type BugSort,
  countByGroup,
  distinctAreas,
  distinctStrategies,
  isFiltering,
  NO_FILTERS,
  visibleBugs,
} from '@/lib/bugFilters'
import {
  acceptanceCriteria,
  BUG_CATEGORIES,
  type BugCategoryKey,
  type BugGroup,
  categoryLabel,
  categoryOfBug,
  evidenceRefs,
  type LifecycleStep,
  lastUpdated,
  lifecycle,
  readableSources,
  reportedFromWorkflow,
  summarizeBugs,
  verificationMethods,
  wasReopened,
} from '@/lib/bugModel'

const GROUPS: { key: BugGroup; label: string; hint: string }[] = [
  { key: 'todo', label: 'Needs fixing', hint: 'Found and confirmed, not fixed yet' },
  {
    key: 'proving',
    label: 'Fixed, awaiting proof',
    hint: 'Changed; a live trading session still has to show it works',
  },
  { key: 'done', label: 'Done', hint: 'Proven by a session, or closed on purpose' },
]

const SORTS: { key: BugSort; label: string }[] = [
  { key: 'severity', label: 'Highest risk first' },
  { key: 'updated', label: 'Recently updated' },
  { key: 'recent', label: 'Seen most recently' },
  { key: 'oldest', label: 'Oldest first' },
  { key: 'frequent', label: 'Seen on most days' },
]

const RISK: Record<BugSeverity, { label: string; meaning: string; tone: Tone }> = {
  HIGH: {
    label: 'High risk',
    meaning: 'Could leave a trade unprotected, send a wrong order, or show wrong P&L',
    tone: 'bad',
  },
  MEDIUM: {
    label: 'Medium risk',
    meaning: 'Wrong records or slower execution, no direct risk to a position',
    tone: 'warn',
  },
  LOW: { label: 'Low risk', meaning: 'Wording, display or noise', tone: 'info' },
}

const KIND: Record<BugKind, string> = {
  BUG: 'Code bug',
  EXECUTION: 'Broker / timing',
  DATA: 'Wrong data',
  'TUNING-IDEA': 'Improvement idea',
}

const STATUS_TEXT: Record<BugStatus, string> = {
  open: 'Needs fixing',
  in_progress: 'Being fixed',
  fixed: 'Awaiting proof',
  verified: 'Verified',
  wontfix: 'Not fixing',
}

const STATUSES = Object.keys(STATUS_TEXT) as BugStatus[]

function statusTone(bug: Bug): Tone {
  if (bug.status === 'open') return bug.severity === 'HIGH' ? 'bad' : 'warn'
  if (bug.status === 'in_progress') return 'info'
  if (bug.status === 'fixed') return 'warn'
  if (bug.status === 'verified') return 'good'
  return 'idle'
}

function shortDate(day: string): string {
  const d = new Date(`${day}T00:00:00`)
  return Number.isNaN(d.getTime())
    ? day
    : d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short' })
}

function Detail({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <div className="text-[13px] font-semibold uppercase text-muted-foreground">{label}</div>
      <div className="text-[15px] whitespace-pre-wrap break-words">{children}</div>
    </div>
  )
}

const STEP_STYLE: Record<LifecycleStep['state'], string> = {
  done: 'text-foreground',
  current: 'font-semibold text-foreground',
  pending: 'text-muted-foreground',
  unrecorded: 'text-muted-foreground',
}

/** Five steps from the persisted status. A step only carries a date when a history entry has one. */
function Lifecycle({ bug, tone }: { bug: Bug; tone: Tone }) {
  const steps = lifecycle(bug)
  if (!steps) {
    const reason = [...bug.history].reverse().find((h) => h.status === 'wontfix')?.note
    return (
      <div className="text-[15px]">
        <StatusBadge tone="idle">Not fixing</StatusBadge>
        {reason && <span className="ml-2 text-muted-foreground">{reason}</span>}
      </div>
    )
  }
  const now = steps.find((x) => x.state === 'current')
  return (
    <div>
      {now && (
        <div className="mb-1.5 text-[15px]">
          Status now: <span className={`font-bold ${TONE_TEXT[tone]}`}>{now.label}</span>
        </div>
      )}
      <ol className="grid grid-cols-5 gap-1" aria-label="Resolution progress">
        {steps.map((s) => (
          <li key={s.key} className="min-w-0">
            <div
              className={`h-1.5 rounded-full ${
                s.state === 'done'
                  ? 'bg-green-600'
                  : s.state === 'current'
                    ? tone === 'info'
                      ? 'bg-blue-500'
                      : tone === 'bad'
                        ? 'bg-red-600'
                        : tone === 'good'
                          ? 'bg-green-600'
                          : 'bg-amber-500'
                    : s.state === 'unrecorded'
                      ? 'border border-dashed bg-transparent'
                      : 'bg-muted'
              }`}
            />
            <div
              className={`mt-1 hidden text-[13px] leading-tight sm:block ${STEP_STYLE[s.state]}`}
            >
              {s.label}
              {s.state === 'current' && <span className="sr-only"> (current)</span>}
            </div>
            <div className="hidden text-[13px] text-muted-foreground sm:block">
              {s.state === 'unrecorded' ? 'not recorded' : s.date ? shortDate(s.date) : ''}
            </div>
          </li>
        ))}
      </ol>
    </div>
  )
}

function BugRow({ bug, defaultOpen }: { bug: Bug; defaultOpen: boolean }) {
  const [toggled, setToggled] = useState(false)
  const open = defaultOpen !== toggled
  const risk = RISK[bug.severity]
  const tone = statusTone(bug)
  const criteria = acceptanceCriteria(bug)
  const verifiedStep = lifecycle(bug)?.find((s) => s.key === 'verified')
  const methods = verificationMethods(bug.verification)
  const refs = evidenceRefs(bug)
  const days = bug.occurrences.length
  const summary = bug.plain || bug.impact
  return (
    <li id={bug.id} className="scroll-mt-4">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setToggled(!toggled)}
        className="flex w-full items-start gap-3 px-4 py-3 text-left hover:bg-muted/50"
      >
        <span className="min-w-0 flex-1 space-y-1">
          <span className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-[15px] font-semibold">{bug.id}</span>
            <StatusBadge tone={risk.tone}>{risk.label}</StatusBadge>
            <StatusBadge tone={tone}>{STATUS_TEXT[bug.status]}</StatusBadge>
            {wasReopened(bug) && <StatusBadge tone="warn">Reopened</StatusBadge>}
          </span>
          <span className="block text-base font-semibold leading-snug">{bug.title}</span>
          {summary && (
            <span className="block text-[15px] leading-relaxed text-muted-foreground">
              {summary}
            </span>
          )}
          <span className="block text-[13px] text-muted-foreground">
            Seen {days} {days === 1 ? 'day' : 'days'}, last {shortDate(bug.last_seen)}
          </span>
        </span>
        <ChevronDown
          className={`mt-1 h-4 w-4 shrink-0 text-muted-foreground transition-transform ${open ? 'rotate-180' : ''}`}
        />
      </button>
      {open && (
        <div className="space-y-3 border-t bg-muted/20 px-4 py-4">
          {bug.plain && bug.impact && (
            <p className="rounded-r-md border-l-4 border-primary/60 bg-muted/40 py-1.5 pl-3 pr-2 text-[15px] leading-relaxed">
              <span className="font-semibold">Trading impact: </span>
              {bug.impact}
            </p>
          )}

          <Lifecycle bug={bug} tone={tone} />

          {criteria && bug.status !== 'verified' && (
            <p className={`rounded-md border border-l-4 px-3 py-2 text-[15px] ${TONE_STYLE.info}`}>
              <span className="font-bold">How we will know it is fixed: </span>
              <span className="text-foreground">{criteria}</span>
            </p>
          )}
          {bug.status === 'verified' && (
            <div
              className={`rounded-md border border-l-4 px-3 py-2 text-[15px] ${TONE_STYLE.good}`}
            >
              <div className="text-foreground">
                <span className="font-bold">Verified</span>{' '}
                {verifiedStep?.date ? shortDate(verifiedStep.date) : '(date not recorded)'}
                {methods.length > 0 && <span> by {methods.join(', ')}</span>}
              </div>
              <div className="text-foreground">
                {bug.verification || 'No verification text was recorded.'}
              </div>
              {refs.length > 0 && (
                <div className="mt-1 break-words font-mono text-[13px] text-muted-foreground">
                  {refs.join('  |  ')}
                </div>
              )}
              {criteria && (
                <div className="mt-1 text-foreground">
                  <span className="font-bold">Criteria met: </span>
                  {criteria}
                </div>
              )}
            </div>
          )}

          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[13px] text-muted-foreground">
            <span>{categoryLabel(categoryOfBug(bug))}</span>
            <span>{KIND[bug.kind]}</span>
            <span>Component: {bug.area}</span>
            {bug.strategies.length > 0 && <span>Strategies: {bug.strategies.join(', ')}</span>}
            <span>Updated {shortDate(lastUpdated(bug).slice(0, 10))}</span>
            <span>
              Fix commit:{' '}
              {bug.fix_commit ? <span className="font-mono">{bug.fix_commit}</span> : 'none'}
            </span>
            {reportedFromWorkflow(bug) && (
              <Link
                to={`/logs/workflow?day=${bug.last_seen}`}
                className="font-medium text-primary hover:underline"
                title="Open the Daily Workflow day this was last seen"
              >
                From Daily Workflow, {shortDate(bug.last_seen)}
              </Link>
            )}
          </div>

          <details className="rounded-md border bg-card">
            <summary className="cursor-pointer px-3 py-2 text-[13px] text-muted-foreground">
              Technical details
            </summary>
            <div className="grid gap-3 border-t p-4 md:grid-cols-2">
              <Detail label="Cause">{bug.root_cause || 'Not found yet'}</Detail>
              <Detail label="Fix">{bug.fix || 'None yet'}</Detail>
              <Detail label="Days seen">{bug.occurrences.join(', ')}</Detail>
              <Detail label="Sources">
                {readableSources(bug).length > 0
                  ? readableSources(bug).join('\n')
                  : 'None recorded'}
              </Detail>
              {bug.evidence.length > 0 && (
                <div className="md:col-span-2">
                  <Detail label="Evidence">
                    <ul className="list-disc pl-5">
                      {bug.evidence.map((item) => (
                        <li key={item}>{item}</li>
                      ))}
                    </ul>
                  </Detail>
                </div>
              )}
              <div className="md:col-span-2">
                <Detail label="History">
                  <ul className="space-y-1 text-muted-foreground">
                    {bug.history.map((entry) => (
                      <li key={`${entry.at}-${entry.status}-${entry.note}`}>
                        {entry.at.replace('T', ' ').slice(0, 16)} - {STATUS_TEXT[entry.status]}:{' '}
                        {entry.note}
                      </li>
                    ))}
                  </ul>
                </Detail>
              </div>
            </div>
          </details>
        </div>
      )}
    </li>
  )
}

function readFilters(params: URLSearchParams): BugFilters {
  const severity = params.get('severity')
  const kind = params.get('kind')
  const status = params.get('status')
  const category = params.get('category')
  return {
    query: params.get('q') ?? '',
    severity: severity && severity in RISK ? (severity as BugSeverity) : 'all',
    kind: kind && kind in KIND ? (kind as BugKind) : 'all',
    area: params.get('area') ?? 'all',
    category:
      category && BUG_CATEGORIES.some((c) => c.key === category)
        ? (category as BugCategoryKey)
        : 'all',
    strategy: params.get('strategy') ?? 'all',
    status: status && STATUSES.includes(status as BugStatus) ? (status as BugStatus) : 'all',
    highRisk: params.get('highrisk') === '1',
  }
}

const FILTER_PARAMS = [
  'q',
  'severity',
  'kind',
  'area',
  'category',
  'strategy',
  'status',
  'highrisk',
]

function FilterSelect({
  label,
  value,
  onChange,
  anyLabel,
  options,
  width = 'w-40',
}: {
  label: string
  value: string
  onChange: (v: string) => void
  anyLabel: string
  options: { value: string; label: string }[]
  width?: string
}) {
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger className={width} aria-label={label}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="all">{anyLabel}</SelectItem>
        {options.map((o) => (
          <SelectItem key={o.value} value={o.value}>
            {o.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

const STATUS_FILL: Record<BugStatus, string> = {
  open: 'bg-red-600',
  in_progress: 'bg-blue-500',
  fixed: 'bg-amber-500',
  verified: 'bg-green-600',
  wontfix: 'bg-muted-foreground/40',
}

/** Where every bug stands, and whether bugs are still happening day to day. */
function Charts({ bugs }: { bugs: Bug[] }) {
  const shares = statusBreakdown(bugs)
  const today = new Date().toLocaleDateString('en-CA')
  const days = seenPerDay(bugs, today, 14)
  const tallest = Math.max(1, ...days.map((d) => d.count))
  const total = days.reduce((n, d) => n + d.count, 0)
  return (
    <Card>
      <CardContent className="grid gap-6 pt-5 md:grid-cols-2">
        <div className="space-y-2">
          <div className="text-[13px] font-semibold uppercase text-muted-foreground">
            Progress, all {bugs.length} bugs
          </div>
          <div className="flex h-3 overflow-hidden rounded-full bg-muted" aria-hidden="true">
            {shares
              .filter((r) => r.count > 0)
              .map((r) => (
                <div
                  key={r.status}
                  className={STATUS_FILL[r.status]}
                  style={{ width: `${r.pct}%` }}
                  title={`${STATUS_TEXT[r.status]}: ${r.count}`}
                />
              ))}
          </div>
          <ul className="flex flex-wrap gap-x-4 gap-y-1 text-[13px]">
            {shares.map((r) => (
              <li key={r.status} className="flex items-center gap-1.5">
                <span className={`inline-block h-2.5 w-2.5 rounded-sm ${STATUS_FILL[r.status]}`} />
                <span className="text-muted-foreground">{STATUS_TEXT[r.status]}</span>
                <span className="font-semibold tabular-nums">{r.count}</span>
              </li>
            ))}
          </ul>
        </div>
        <div className="space-y-2">
          <div className="flex items-baseline justify-between">
            <span className="text-[13px] font-semibold uppercase text-muted-foreground">
              Bugs seen per day, last 14 days
            </span>
            <span className="text-[13px] text-muted-foreground">
              {total === 0 ? 'none seen' : `${total} sightings`}
            </span>
          </div>
          <div className="flex h-16 items-end gap-1">
            {days.map((d) => (
              <div
                key={d.day}
                className="flex h-full flex-1 flex-col justify-end"
                title={`${d.day}: ${d.count} ${d.count === 1 ? 'bug' : 'bugs'} seen${
                  d.high ? `, ${d.high} high risk` : ''
                }`}
              >
                <div
                  className={d.high ? 'rounded-sm bg-red-600' : 'rounded-sm bg-amber-500'}
                  style={{ height: `${(d.count / tallest) * 100}%`, minHeight: d.count ? 3 : 0 }}
                />
              </div>
            ))}
          </div>
          <div className="flex justify-between text-[13px] text-muted-foreground">
            <span>{shortDate(days[0].day)}</span>
            <span className="flex items-center gap-3">
              <span className="flex items-center gap-1">
                <span className="inline-block h-2.5 w-2.5 rounded-sm bg-red-600" />
                includes high risk
              </span>
              <span className="flex items-center gap-1">
                <span className="inline-block h-2.5 w-2.5 rounded-sm bg-amber-500" />
                other
              </span>
            </span>
            <span>today</span>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

export default function BugTracker() {
  const [params, setParams] = useSearchParams()
  const [showFilters, setShowFilters] = useState(false)
  const filters = readFilters(params)
  const groupParam = params.get('group')
  const group: BugGroup = GROUPS.some((g) => g.key === groupParam)
    ? (groupParam as BugGroup)
    : 'todo'
  const sortParam = params.get('sort')
  const sort: BugSort = SORTS.some((s) => s.key === sortParam) ? (sortParam as BugSort) : 'severity'

  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ['bug-tracker'],
    queryFn: bugsApi.list,
    refetchInterval: 60_000,
  })
  const all = useMemo(() => data ?? [], [data])

  const setParam = (key: string, value: string, fallback: string) => {
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev)
        if (value === fallback || value === '') next.delete(key)
        else next.set(key, value)
        return next
      },
      { replace: true }
    )
  }

  const counts = useMemo(() => countByGroup(all, filters), [all, filters])
  const totals = useMemo(() => countByGroup(all, NO_FILTERS), [all])
  const summary = useMemo(() => summarizeBugs(all), [all])
  const areas = useMemo(() => distinctAreas(all), [all])
  const strategies = useMemo(() => distinctStrategies(all), [all])
  const categories = useMemo(() => {
    const present = new Set(all.map(categoryOfBug))
    return BUG_CATEGORIES.filter((c) => present.has(c.key))
  }, [all])
  const shown = useMemo(() => visibleBugs(all, group, filters, sort), [all, group, filters, sort])
  const filtering = isFiltering(filters)
  // Filters that are active stay visible; otherwise they hide behind one button.
  const filtersOpen = showFilters || filters.severity !== 'all' || filters.highRisk

  const sub: Record<BugGroup, string> = {
    todo: summary.highNeedsFix ? `${summary.highNeedsFix} high risk` : '',
    proving: summary.highAwaitingProof ? `${summary.highAwaitingProof} high risk` : '',
    done: '',
  }
  const subTone: Record<BugGroup, string> = {
    todo: summary.highNeedsFix ? TONE_TEXT.bad : '',
    proving: summary.highAwaitingProof ? TONE_TEXT.warn : '',
    done: '',
  }

  return (
    <div className="py-6 space-y-5">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <BugIcon className="h-6 w-6" />
            Bug Tracker
          </h1>
          <p className="text-muted-foreground mt-1">
            What is broken, what is fixed, what needs proof. To fix one, ask Claude: /resolve
            BUG-007
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={() => refetch()} disabled={isFetching}>
          <RefreshCw className={`h-4 w-4 mr-2 ${isFetching ? 'animate-spin' : ''}`} />
          Refresh
        </Button>
      </div>

      <div className="grid grid-cols-3 gap-2 sm:gap-3">
        {GROUPS.map((g) => (
          <button
            type="button"
            key={g.key}
            aria-pressed={group === g.key}
            title={g.hint}
            onClick={() => setParam('group', g.key, 'todo')}
            className={`text-left rounded-lg border p-3 sm:p-4 transition-colors ${
              group === g.key
                ? 'border-primary ring-1 ring-primary bg-primary/5'
                : 'hover:bg-muted/50'
            }`}
          >
            <div className="text-2xl sm:text-3xl font-bold tabular-nums">
              {counts[g.key]}
              {filtering && (
                <span className="ml-1 text-[15px] font-normal text-muted-foreground">
                  of {totals[g.key]}
                </span>
              )}
            </div>
            <div className="font-semibold">{g.label}</div>
            {data && sub[g.key] && (
              <div className={`text-[13px] font-medium ${subTone[g.key]}`}>{sub[g.key]}</div>
            )}
          </button>
        ))}
      </div>

      {data && all.length > 0 && <Charts bugs={all} />}

      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative flex-1 min-w-52">
            <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
            <Input
              value={filters.query}
              onChange={(e) => setParam('q', e.target.value, '')}
              placeholder="Search id, words, strategy or component"
              className="pl-8"
              aria-label="Search bugs"
            />
          </div>
          <Button
            variant={filtersOpen ? 'secondary' : 'outline'}
            size="sm"
            aria-expanded={filtersOpen}
            onClick={() => setShowFilters(!showFilters)}
          >
            <SlidersHorizontal className="h-4 w-4 mr-1" />
            Filters and sort
          </Button>
          {filtering && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() =>
                setParams(
                  (prev) => {
                    const next = new URLSearchParams(prev)
                    for (const k of FILTER_PARAMS) next.delete(k)
                    return next
                  },
                  { replace: true }
                )
              }
            >
              <X className="h-4 w-4 mr-1" />
              Clear
            </Button>
          )}
        </div>
        {filtersOpen && (
          <div className="flex flex-wrap items-center gap-2">
            <Select value={sort} onValueChange={(v) => setParam('sort', v, 'severity')}>
              <SelectTrigger className="w-48" aria-label="Sort">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {SORTS.map((o) => (
                  <SelectItem key={o.key} value={o.key}>
                    {o.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <FilterSelect
              label="Risk"
              value={filters.highRisk ? 'HIGH' : filters.severity}
              onChange={(v) => {
                setParam('highrisk', '', '')
                setParam('severity', v, 'all')
              }}
              anyLabel="Any risk"
              options={(Object.keys(RISK) as BugSeverity[]).map((k) => ({
                value: k,
                label: RISK[k].label,
              }))}
              width="w-36"
            />
            <FilterSelect
              label="Category"
              value={filters.category}
              onChange={(v) => setParam('category', v, 'all')}
              anyLabel="Any category"
              options={categories.map((c) => ({ value: c.key, label: c.label }))}
              width="w-48"
            />
            <FilterSelect
              label="Status"
              value={filters.status}
              onChange={(v) => setParam('status', v, 'all')}
              anyLabel="Any status"
              options={STATUSES.map((st) => ({ value: st, label: STATUS_TEXT[st] }))}
              width="w-44"
            />
            {strategies.length > 0 && (
              <FilterSelect
                label="Strategy"
                value={filters.strategy}
                onChange={(v) => setParam('strategy', v, 'all')}
                anyLabel="Any strategy"
                options={strategies.map((st) => ({ value: st, label: st }))}
                width="w-48"
              />
            )}
            {areas.length > 1 && (
              <FilterSelect
                label="Component"
                value={filters.area}
                onChange={(v) => setParam('area', v, 'all')}
                anyLabel="Any component"
                options={areas.map((a) => ({ value: a, label: a }))}
              />
            )}
            <FilterSelect
              label="Type"
              value={filters.kind}
              onChange={(v) => setParam('kind', v, 'all')}
              anyLabel="Any type"
              options={(Object.keys(KIND) as BugKind[]).map((k) => ({ value: k, label: KIND[k] }))}
            />
          </div>
        )}
      </div>

      {isLoading ? (
        <Skeleton className="h-64 w-full" />
      ) : error ? (
        <Card>
          <CardContent className="pt-6 text-[15px]">{(error as Error).message}</CardContent>
        </Card>
      ) : shown.length === 0 ? (
        <Card>
          <CardContent className="pt-6 text-[15px] text-muted-foreground">
            {filtering
              ? 'No bug in this tab matches the search and filters. Clear them to see all.'
              : 'Nothing here.'}
          </CardContent>
        </Card>
      ) : (
        <ul className="divide-y rounded-lg border bg-card">
          {shown.map((bug) => (
            <BugRow
              key={bug.id}
              bug={bug}
              defaultOpen={shown.length === 1 && filters.query !== ''}
            />
          ))}
        </ul>
      )}
    </div>
  )
}
