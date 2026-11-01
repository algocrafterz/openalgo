import { useQuery } from '@tanstack/react-query'
import { Bug as BugIcon, ChevronDown, ChevronRight, RefreshCw } from 'lucide-react'
import { useMemo, useState } from 'react'
import { type Bug, type BugKind, type BugSeverity, type BugStatus, bugsApi } from '@/api/bugs'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'

type Group = 'todo' | 'proving' | 'done'

const GROUPS: { key: Group; label: string; hint: string; statuses: BugStatus[] }[] = [
  {
    key: 'todo',
    label: 'Needs fixing',
    hint: 'Found and confirmed, not fixed yet',
    statuses: ['open', 'in_progress'],
  },
  {
    key: 'proving',
    label: 'Fixed, waiting for proof',
    hint: 'Changed; a trading session still has to show it works',
    statuses: ['fixed'],
  },
  {
    key: 'done',
    label: 'Done',
    hint: 'Proven by a session, or closed on purpose',
    statuses: ['verified', 'wontfix'],
  },
]

const RISK: Record<BugSeverity, { label: string; meaning: string; className: string }> = {
  HIGH: {
    label: 'High risk',
    meaning: 'Could leave a trade unprotected, send a wrong order, or show wrong P&L',
    className: 'bg-red-600 text-white',
  },
  MEDIUM: {
    label: 'Medium',
    meaning: 'Wrong records or slower execution, no direct risk to a position',
    className: 'bg-amber-500 text-white',
  },
  LOW: {
    label: 'Low',
    meaning: 'Wording, display or noise',
    className: 'bg-secondary text-secondary-foreground',
  },
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
  fixed: 'Fixed, waiting for proof',
  verified: 'Done, proven',
  wontfix: 'Closed, not fixing',
}

const SEVERITY_RANK: Record<BugSeverity, number> = { HIGH: 0, MEDIUM: 1, LOW: 2 }

function groupOf(bug: Bug): Group {
  return GROUPS.find((g) => g.statuses.includes(bug.status))?.key ?? 'todo'
}

function shortDate(day: string): string {
  const d = new Date(`${day}T00:00:00`)
  return d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short' })
}

/** The "how we'll know" event noted by /resolve verify ("... Proves it: <event>"). */
function proofEvent(bug: Bug): string | null {
  for (const entry of [...bug.history].reverse()) {
    const at = entry.note.indexOf('Proves it:')
    if (at >= 0) return entry.note.slice(at + 'Proves it:'.length).trim()
  }
  return null
}

function Detail({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-xs font-semibold uppercase text-muted-foreground">{label}</div>
      <div className="text-sm whitespace-pre-wrap break-words">{children}</div>
    </div>
  )
}

function BugCard({ bug }: { bug: Bug }) {
  const [open, setOpen] = useState(false)
  const risk = RISK[bug.severity]
  const proof = bug.status === 'fixed' ? proofEvent(bug) : null
  const days = bug.occurrences.length
  return (
    <Card>
      <CardContent className="pt-5 space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <Badge className={risk.className} title={risk.meaning}>
            {risk.label}
          </Badge>
          <Badge variant="outline">{KIND[bug.kind]}</Badge>
          <Badge variant="secondary">{STATUS_TEXT[bug.status]}</Badge>
          <span className="ml-auto text-xs text-muted-foreground">
            {bug.id} | seen {days} {days === 1 ? 'day' : 'days'}, last {shortDate(bug.last_seen)}
          </span>
        </div>
        <p className="text-sm leading-relaxed">{bug.plain || bug.title}</p>
        {proof && (
          <p className="text-sm rounded-md bg-muted/50 px-3 py-2">
            <span className="font-medium">How we will know it is fixed: </span>
            {proof}
          </p>
        )}
        {bug.status === 'verified' && bug.verification && (
          <p className="text-sm rounded-md bg-green-600/10 px-3 py-2">
            <span className="font-medium">Proven: </span>
            {bug.verification}
          </p>
        )}
        <button
          type="button"
          onClick={() => setOpen(!open)}
          className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
        >
          {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
          Technical details
        </button>
        {open && (
          <div className="grid gap-3 md:grid-cols-2 rounded-md border p-4">
            <div className="md:col-span-2">
              <Detail label="Title">{bug.title}</Detail>
            </div>
            <Detail label="What it costs">{bug.impact}</Detail>
            <Detail label="Cause">{bug.root_cause || 'Not found yet'}</Detail>
            <Detail label="Fix">
              {bug.fix || 'None yet'}
              {bug.fix_commit ? ` (${bug.fix_commit})` : ''}
            </Detail>
            <Detail label="Area and strategies">
              {bug.area}
              {bug.strategies.length > 0 ? ` | ${bug.strategies.join(', ')}` : ''}
            </Detail>
            <Detail label="Days seen">{bug.occurrences.join(', ')}</Detail>
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
                    <li key={`${entry.at}-${entry.note}`}>
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

export default function BugTracker() {
  const [group, setGroup] = useState<Group>('todo')
  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ['bug-tracker'],
    queryFn: bugsApi.list,
  })
  const counts = useMemo(() => {
    const all = data ?? []
    return Object.fromEntries(
      GROUPS.map((g) => [g.key, all.filter((b) => groupOf(b) === g.key).length])
    ) as Record<Group, number>
  }, [data])
  const highOpen = (data ?? []).filter((b) => groupOf(b) === 'todo' && b.severity === 'HIGH').length
  const shown = useMemo(
    () =>
      (data ?? [])
        .filter((b) => groupOf(b) === group)
        .sort(
          (a, b) =>
            SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity] ||
            b.last_seen.localeCompare(a.last_seen)
        ),
    [data, group]
  )

  return (
    <div className="py-6 space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <BugIcon className="h-6 w-6" />
            Bug Tracker
          </h1>
          <p className="text-muted-foreground mt-1">
            Problems the daily checks found, in plain words. To fix one, ask Claude: /resolve
            BUG-007, or /resolve for the next one.
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={() => refetch()} disabled={isFetching}>
          <RefreshCw className={`h-4 w-4 mr-2 ${isFetching ? 'animate-spin' : ''}`} />
          Refresh
        </Button>
      </div>

      {data && (
        <p className="text-sm">
          {counts.todo === 0
            ? 'Nothing needs fixing right now.'
            : `${counts.todo} ${counts.todo === 1 ? 'problem needs' : 'problems need'} fixing${
                highOpen ? `, ${highOpen} of them high risk` : ''
              }.`}{' '}
          {counts.proving > 0 &&
            `${counts.proving} fixed ${counts.proving === 1 ? 'one is' : 'ones are'} waiting for a trading session to prove ${counts.proving === 1 ? 'it' : 'them'}.`}
        </p>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        {GROUPS.map((g) => (
          <button
            type="button"
            key={g.key}
            onClick={() => setGroup(g.key)}
            className={`text-left rounded-lg border p-4 transition-colors ${
              group === g.key
                ? 'border-primary ring-1 ring-primary bg-primary/5'
                : 'hover:bg-muted/50'
            }`}
          >
            <div className="text-2xl font-bold">{counts[g.key] ?? 0}</div>
            <div className="font-medium">{g.label}</div>
            <div className="text-xs text-muted-foreground">{g.hint}</div>
          </button>
        ))}
      </div>

      {isLoading ? (
        <Skeleton className="h-64 w-full" />
      ) : error ? (
        <Card>
          <CardContent className="pt-6 text-sm">{(error as Error).message}</CardContent>
        </Card>
      ) : shown.length === 0 ? (
        <Card>
          <CardContent className="pt-6 text-sm text-muted-foreground">Nothing here.</CardContent>
        </Card>
      ) : (
        <div className="space-y-3">
          {shown.map((bug) => (
            <BugCard key={bug.id} bug={bug} />
          ))}
        </div>
      )}
    </div>
  )
}
