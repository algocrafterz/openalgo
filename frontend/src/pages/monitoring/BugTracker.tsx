import { useQuery } from '@tanstack/react-query'
import {
  Bug as BugIcon,
  ChevronDown,
  ChevronRight,
  LayoutGrid,
  RefreshCw,
  Rows3,
  Search,
  X,
} from 'lucide-react'
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
  open: 'Confirmed, needs fixing',
  in_progress: 'Confirmed, being fixed',
  fixed: 'Fixed, awaiting proof',
  verified: 'Verified',
  wontfix: 'Closed, not fixing',
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
        <StatusBadge tone="idle">Closed, not fixing</StatusBadge>
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

function BugCard({ bug, expandAll }: { bug: Bug; expandAll: boolean }) {
  const [toggled, setToggled] = useState(false)
  const open = expandAll !== toggled
  const risk = RISK[bug.severity]
  const tone = statusTone(bug)
  const criteria = acceptanceCriteria(bug)
  const verifiedStep = lifecycle(bug)?.find((s) => s.key === 'verified')
  const methods = verificationMethods(bug.verification)
  const refs = evidenceRefs(bug)
  const days = bug.occurrences.length
  return (
    <Card id={bug.id}>
      <CardContent className="pt-5 space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-[15px] font-semibold">{bug.id}</span>
          <StatusBadge tone={risk.tone}>{risk.label}</StatusBadge>
          <StatusBadge tone={tone}>{STATUS_TEXT[bug.status]}</StatusBadge>
          {wasReopened(bug) && <StatusBadge tone="warn">Reopened</StatusBadge>}
          <span className="ml-auto text-[13px] text-muted-foreground">
            {categoryLabel(categoryOfBug(bug))} | {KIND[bug.kind]}
          </span>
        </div>

        <h3 className="text-lg font-bold leading-snug">{bug.title}</h3>

        {bug.impact && (
          <p className="rounded-r-md border-l-4 border-primary/60 bg-muted/40 py-1.5 pl-3 pr-2 text-base leading-relaxed">
            <span className="font-semibold">Trading impact: </span>
            {bug.impact}
          </p>
        )}
        {bug.plain && (
          <p className="text-[15px] leading-relaxed text-muted-foreground">{bug.plain}</p>
        )}

        <Lifecycle bug={bug} tone={tone} />

        {criteria && bug.status !== 'verified' && (
          <p className={`rounded-md border border-l-4 px-3 py-2 text-base ${TONE_STYLE.info}`}>
            <span className="font-bold">Acceptance criteria, how we will know it is fixed: </span>
            <span className="text-foreground">{criteria}</span>
          </p>
        )}
        {bug.status === 'verified' && (
          <dl
            className={`grid gap-x-6 gap-y-2 rounded-md border border-l-4 px-3 py-2 text-[15px] sm:grid-cols-2 ${TONE_STYLE.good}`}
          >
            <div>
              <dt className="font-bold">Verification date</dt>
              <dd className="text-foreground">
                {verifiedStep?.date ? shortDate(verifiedStep.date) : 'Not recorded'}
              </dd>
            </div>
            <div>
              <dt className="font-bold">Verification method</dt>
              <dd className="text-foreground">
                {methods.length > 0 ? methods.join(', ') : 'Not recorded'}
                <span className="block text-[13px] text-muted-foreground">
                  Read from the evidence text; the tracker has no method field.
                </span>
              </dd>
            </div>
            <div className="sm:col-span-2">
              <dt className="font-bold">Evidence</dt>
              <dd className="text-foreground">
                {bug.verification || 'No verification text was recorded.'}
                {refs.length > 0 && (
                  <span className="mt-1 block break-words font-mono text-[13px] text-muted-foreground">
                    {refs.join('  |  ')}
                  </span>
                )}
              </dd>
            </div>
            <div className="sm:col-span-2">
              <dt className="font-bold">Acceptance criteria satisfied</dt>
              <dd className="text-foreground">
                {criteria ?? 'No acceptance criteria were recorded for this bug.'}
              </dd>
            </div>
          </dl>
        )}
        {bug.status === 'fixed' && !criteria && (
          <p className="text-[15px] text-muted-foreground">
            No acceptance criteria are recorded for this fix yet. When it is verified, the evidence
            that shows it works has to be recorded with it.
          </p>
        )}

        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[13px] text-muted-foreground">
          <span>
            Seen {days} {days === 1 ? 'day' : 'days'}, last {shortDate(bug.last_seen)}
          </span>
          <span>Updated {shortDate(lastUpdated(bug).slice(0, 10))}</span>
          {bug.strategies.length > 0 && <span>Strategies: {bug.strategies.join(', ')}</span>}
          <span>Component: {bug.area}</span>
          {reportedFromWorkflow(bug) && (
            <Link
              to={`/logs/workflow?day=${bug.last_seen}`}
              className="font-medium text-primary hover:underline"
              title="Open the Daily Workflow day this was last seen"
            >
              From Daily Workflow, {shortDate(bug.last_seen)}
            </Link>
          )}
          <span>
            Fix commit:{' '}
            {bug.fix_commit ? <span className="font-mono">{bug.fix_commit}</span> : 'none recorded'}
          </span>
        </div>

        <button
          type="button"
          aria-expanded={open}
          onClick={() => setToggled(!toggled)}
          className="flex items-center gap-1 text-[13px] text-muted-foreground hover:text-foreground"
        >
          {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
          Technical details
        </button>
        {open && (
          <div className="grid gap-3 md:grid-cols-2 rounded-md border p-4">
            <Detail label="Cause">{bug.root_cause || 'Not found yet'}</Detail>
            <Detail label="Fix">{bug.fix || 'None yet'}</Detail>
            <Detail label="Days seen">{bug.occurrences.join(', ')}</Detail>
            <Detail label="Sources">
              {readableSources(bug).length > 0 ? readableSources(bug).join('\n') : 'None recorded'}
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
        )}
      </CardContent>
    </Card>
  )
}

function BugTable({ bugs, onOpen }: { bugs: Bug[]; onOpen: (id: string) => void }) {
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full text-[15px]">
        <thead>
          <tr className="border-b bg-muted/30 text-[13px] text-muted-foreground">
            <th className="px-3 py-2 text-left font-medium">ID</th>
            <th className="px-3 py-2 text-left font-medium">Risk</th>
            <th className="px-3 py-2 text-left font-medium">Problem</th>
            <th className="px-3 py-2 text-left font-medium">Category</th>
            <th className="px-3 py-2 text-left font-medium">Status</th>
            <th className="px-3 py-2 text-left font-medium">Strategies</th>
            <th className="px-3 py-2 text-left font-medium">Last seen</th>
            <th className="px-3 py-2 text-left font-medium">Updated</th>
          </tr>
        </thead>
        <tbody>
          {bugs.map((b) => (
            <tr key={b.id} className="border-b last:border-0 align-top">
              <td className="px-3 py-2 font-mono whitespace-nowrap">{b.id}</td>
              <td className="px-3 py-2">
                <StatusBadge tone={RISK[b.severity].tone}>{RISK[b.severity].label}</StatusBadge>
              </td>
              <td className="px-3 py-2 min-w-[18rem]">
                <button
                  type="button"
                  className="text-left hover:underline"
                  onClick={() => onOpen(b.id)}
                >
                  {b.title}
                </button>
              </td>
              <td className="px-3 py-2 whitespace-nowrap">{categoryLabel(categoryOfBug(b))}</td>
              <td className="px-3 py-2">
                <StatusBadge tone={statusTone(b)}>{STATUS_TEXT[b.status]}</StatusBadge>
              </td>
              <td className="px-3 py-2 text-muted-foreground">
                {b.strategies.length > 0 ? b.strategies.join(', ') : 'none'}
              </td>
              <td className="px-3 py-2 whitespace-nowrap">{shortDate(b.last_seen)}</td>
              <td className="px-3 py-2 whitespace-nowrap">
                {shortDate(lastUpdated(b).slice(0, 10))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
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

export default function BugTracker() {
  const [params, setParams] = useSearchParams()
  const [expandAll, setExpandAll] = useState(false)
  const filters = readFilters(params)
  const groupParam = params.get('group')
  const group: BugGroup = GROUPS.some((g) => g.key === groupParam)
    ? (groupParam as BugGroup)
    : 'todo'
  const sortParam = params.get('sort')
  const sort: BugSort = SORTS.some((s) => s.key === sortParam) ? (sortParam as BugSort) : 'severity'
  const view = params.get('view') === 'table' ? 'table' : 'cards'

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

  const sub: Record<BugGroup, string> = {
    todo: `${summary.highNeedsFix} high risk`,
    proving: `${summary.highAwaitingProof} high risk, waiting for a live session`,
    done: `${summary.verified} verified, ${summary.closedNotFixing} closed not fixing`,
  }
  const subTone: Record<BugGroup, string> = {
    todo: summary.highNeedsFix ? TONE_TEXT.bad : '',
    proving: summary.highAwaitingProof ? TONE_TEXT.warn : '',
    done: '',
  }

  return (
    <div className="py-6 space-y-6">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <BugIcon className="h-6 w-6" />
            Bug Tracker
          </h1>
          <p className="text-muted-foreground mt-1">
            What is broken, what has been fixed, and what still needs proof. To fix one, ask Claude:
            /resolve BUG-007, or /resolve for the next one.
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={() => refetch()} disabled={isFetching}>
          <RefreshCw className={`h-4 w-4 mr-2 ${isFetching ? 'animate-spin' : ''}`} />
          Refresh
        </Button>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        {GROUPS.map((g) => (
          <button
            type="button"
            key={g.key}
            aria-pressed={group === g.key}
            onClick={() => setParam('group', g.key, 'todo')}
            className={`text-left rounded-lg border p-4 transition-colors ${
              group === g.key
                ? 'border-primary ring-1 ring-primary bg-primary/5'
                : 'hover:bg-muted/50'
            }`}
          >
            <div className="text-3xl font-bold tabular-nums">
              {counts[g.key]}
              {filtering && (
                <span className="ml-1 text-[15px] font-normal text-muted-foreground">
                  of {totals[g.key]}
                </span>
              )}
            </div>
            <div className="font-semibold">{g.label}</div>
            <div className="text-[13px] text-muted-foreground">{g.hint}</div>
            {data && (
              <div className={`mt-1 text-[13px] font-medium ${subTone[g.key]}`}>{sub[g.key]}</div>
            )}
          </button>
        ))}
      </div>

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
            variant={filters.highRisk ? 'default' : 'outline'}
            size="sm"
            aria-pressed={filters.highRisk}
            onClick={() => setParam('highrisk', filters.highRisk ? '' : '1', '')}
          >
            High risk only
          </Button>
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
          <fieldset className="m-0 flex min-w-0 rounded-md border p-0">
            <legend className="sr-only">View</legend>
            <Button
              variant={view === 'cards' ? 'secondary' : 'ghost'}
              size="sm"
              aria-pressed={view === 'cards'}
              onClick={() => setParam('view', 'cards', 'cards')}
            >
              <LayoutGrid className="h-4 w-4 mr-1" />
              Cards
            </Button>
            <Button
              variant={view === 'table' ? 'secondary' : 'ghost'}
              size="sm"
              aria-pressed={view === 'table'}
              onClick={() => setParam('view', 'table', 'cards')}
            >
              <Rows3 className="h-4 w-4 mr-1" />
              Table
            </Button>
          </fieldset>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <FilterSelect
            label="Risk"
            value={filters.severity}
            onChange={(v) => setParam('severity', v, 'all')}
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
            options={STATUSES.map((s) => ({ value: s, label: STATUS_TEXT[s] }))}
            width="w-52"
          />
          {strategies.length > 0 && (
            <FilterSelect
              label="Strategy"
              value={filters.strategy}
              onChange={(v) => setParam('strategy', v, 'all')}
              anyLabel="Any strategy"
              options={strategies.map((s) => ({ value: s, label: s }))}
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
          {view === 'cards' && (
            <Button
              variant="outline"
              size="sm"
              className="ml-auto"
              onClick={() => setExpandAll(!expandAll)}
            >
              {expandAll ? 'Collapse all' : 'Expand all'}
            </Button>
          )}
        </div>
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
      ) : view === 'table' ? (
        <BugTable
          bugs={shown}
          onOpen={(id) =>
            setParams(
              (prev) => {
                const next = new URLSearchParams(prev)
                next.delete('view')
                next.set('q', id)
                return next
              },
              { replace: true }
            )
          }
        />
      ) : (
        <div className="space-y-3">
          {shown.map((bug) => (
            <BugCard key={bug.id} bug={bug} expandAll={expandAll} />
          ))}
        </div>
      )}
    </div>
  )
}
