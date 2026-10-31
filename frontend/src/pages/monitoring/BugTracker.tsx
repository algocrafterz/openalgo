import { useQuery } from '@tanstack/react-query'
import { Bug as BugIcon, ChevronDown, ChevronRight, RefreshCw } from 'lucide-react'
import { Fragment, useMemo, useState } from 'react'
import { type Bug, type BugSeverity, type BugStatus, bugsApi } from '@/api/bugs'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'

type StatusFilter = 'active' | 'all' | BugStatus

const STATUS_FILTERS: { value: StatusFilter; label: string }[] = [
  { value: 'active', label: 'Needs work' },
  { value: 'open', label: 'Open' },
  { value: 'fixed', label: 'Fixed, awaiting proof' },
  { value: 'verified', label: 'Verified' },
  { value: 'all', label: 'All' },
]

const SEVERITY_RANK: Record<BugSeverity, number> = { HIGH: 0, MEDIUM: 1, LOW: 2 }

const STATUS_LABEL: Record<BugStatus, string> = {
  open: 'Open',
  in_progress: 'In progress',
  fixed: 'Fixed, awaiting proof',
  verified: 'Verified',
  wontfix: "Won't fix",
}

function severityVariant(severity: BugSeverity) {
  if (severity === 'HIGH') return 'destructive' as const
  if (severity === 'MEDIUM') return 'default' as const
  return 'secondary' as const
}

function statusClass(status: BugStatus) {
  if (status === 'verified') return 'bg-green-600 text-white'
  if (status === 'fixed') return 'bg-blue-600 text-white'
  if (status === 'wontfix') return 'bg-muted text-muted-foreground'
  return 'bg-amber-500 text-white'
}

function matchesStatus(bug: Bug, filter: StatusFilter) {
  if (filter === 'all') return true
  if (filter === 'active') return bug.status !== 'verified' && bug.status !== 'wontfix'
  return bug.status === filter
}

function Detail({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-xs font-semibold uppercase text-muted-foreground">{label}</div>
      <div className="text-sm whitespace-pre-wrap">{children}</div>
    </div>
  )
}

function BugDetails({ bug }: { bug: Bug }) {
  return (
    <div className="grid gap-3 p-4 md:grid-cols-2">
      <Detail label="What it costs">{bug.impact}</Detail>
      <Detail label="Cause">{bug.root_cause || 'Not found yet'}</Detail>
      <Detail label="Fix">
        {bug.fix || 'None yet'}
        {bug.fix_commit ? ` (${bug.fix_commit})` : ''}
      </Detail>
      <Detail label="Proof it is fixed">{bug.verification || 'Not yet proven'}</Detail>
      <Detail label="Days seen">{bug.occurrences.join(', ')}</Detail>
      <Detail label="Strategies">{bug.strategies.join(', ') || 'All'}</Detail>
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
      {bug.sources.length > 0 && (
        <div className="md:col-span-2">
          <Detail label="Reports">{bug.sources.join('\n')}</Detail>
        </div>
      )}
      <div className="md:col-span-2">
        <Detail label="History">
          <ul className="space-y-1">
            {bug.history.map((entry) => (
              <li key={`${entry.at}-${entry.note}`} className="text-muted-foreground">
                {entry.at.replace('T', ' ').slice(0, 16)} - {STATUS_LABEL[entry.status]}:{' '}
                {entry.note}
              </li>
            ))}
          </ul>
        </Detail>
      </div>
    </div>
  )
}

export default function BugTracker() {
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('active')
  const [expanded, setExpanded] = useState<string | null>(null)
  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ['bug-tracker'],
    queryFn: bugsApi.list,
  })

  const bugs = useMemo(
    () =>
      (data ?? [])
        .filter((bug) => matchesStatus(bug, statusFilter))
        .sort(
          (a, b) =>
            SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity] ||
            b.last_seen.localeCompare(a.last_seen)
        ),
    [data, statusFilter]
  )

  const counts = useMemo(() => {
    const all = data ?? []
    return {
      high: all.filter((b) => b.severity === 'HIGH' && matchesStatus(b, 'active')).length,
      active: all.filter((b) => matchesStatus(b, 'active')).length,
      fixed: all.filter((b) => b.status === 'fixed').length,
      verified: all.filter((b) => b.status === 'verified').length,
    }
  }, [data])

  return (
    <div className="py-6 space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <BugIcon className="h-6 w-6" />
            Bug Tracker
          </h1>
          <p className="text-muted-foreground mt-1">
            Problems found in the daily reviews, what they cost, and whether the fix is proven. To
            work one, ask Claude: /resolve BUG-007 (or /resolve for the next one).
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={() => refetch()} disabled={isFetching}>
          <RefreshCw className={`h-4 w-4 mr-2 ${isFetching ? 'animate-spin' : ''}`} />
          Refresh
        </Button>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {[
          { label: 'High severity, needs work', value: counts.high },
          { label: 'Needs work', value: counts.active },
          { label: 'Fixed, awaiting proof', value: counts.fixed },
          { label: 'Verified', value: counts.verified },
        ].map((tile) => (
          <Card key={tile.label}>
            <CardContent className="pt-6">
              <div className="text-2xl font-bold">{tile.value}</div>
              <div className="text-sm text-muted-foreground">{tile.label}</div>
            </CardContent>
          </Card>
        ))}
      </div>

      <div className="flex flex-wrap gap-2">
        {STATUS_FILTERS.map((option) => (
          <Button
            key={option.value}
            size="sm"
            variant={statusFilter === option.value ? 'default' : 'outline'}
            onClick={() => setStatusFilter(option.value)}
          >
            {option.label}
          </Button>
        ))}
      </div>

      {isLoading ? (
        <Skeleton className="h-64 w-full" />
      ) : error ? (
        <Card>
          <CardContent className="pt-6 text-sm">{(error as Error).message}</CardContent>
        </Card>
      ) : bugs.length === 0 ? (
        <Card>
          <CardContent className="pt-6 text-sm text-muted-foreground">
            Nothing here. Bugs are recorded by the end-of-day and paper reviews.
          </CardContent>
        </Card>
      ) : (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-8" />
                <TableHead>ID</TableHead>
                <TableHead>Problem</TableHead>
                <TableHead>Severity</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Last seen</TableHead>
                <TableHead className="text-right">Days</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {bugs.map((bug) => {
                const open = expanded === bug.id
                return (
                  <Fragment key={bug.id}>
                    <TableRow
                      className="cursor-pointer"
                      onClick={() => setExpanded(open ? null : bug.id)}
                    >
                      <TableCell>
                        {open ? (
                          <ChevronDown className="h-4 w-4" />
                        ) : (
                          <ChevronRight className="h-4 w-4" />
                        )}
                      </TableCell>
                      <TableCell className="font-mono text-xs">{bug.id}</TableCell>
                      <TableCell className="max-w-md whitespace-normal">
                        <div className="font-medium">{bug.title}</div>
                        <div className="text-xs text-muted-foreground">
                          {bug.area}
                          {bug.strategies.length > 0 ? ` - ${bug.strategies.join(', ')}` : ''}
                        </div>
                      </TableCell>
                      <TableCell>
                        <Badge variant={severityVariant(bug.severity)}>{bug.severity}</Badge>
                      </TableCell>
                      <TableCell>
                        <Badge variant="outline">{bug.kind}</Badge>
                      </TableCell>
                      <TableCell>
                        <Badge className={statusClass(bug.status)}>
                          {STATUS_LABEL[bug.status]}
                        </Badge>
                      </TableCell>
                      <TableCell>{bug.last_seen}</TableCell>
                      <TableCell className="text-right">{bug.occurrences.length}</TableCell>
                    </TableRow>
                    {open && (
                      <TableRow>
                        <TableCell colSpan={8} className="bg-muted/40 p-0 whitespace-normal">
                          <BugDetails bug={bug} />
                        </TableCell>
                      </TableRow>
                    )}
                  </Fragment>
                )
              })}
            </TableBody>
          </Table>
        </Card>
      )}
    </div>
  )
}
