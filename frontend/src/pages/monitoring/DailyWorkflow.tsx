import { useQuery } from '@tanstack/react-query'
import { CalendarCheck, FileText, RefreshCw } from 'lucide-react'
import { useState } from 'react'
import {
  opsWorkflowApi,
  type WorkflowCheckResult,
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'

const PHASE_INFO: Record<WorkflowPhase['phase'], { title: string; when: string; duty: string }> = {
  'open-day': {
    title: 'Open',
    when: '09:12',
    duty: 'Is the stack ready: services, broker session, feed, clock, overnight jobs',
  },
  liveness: {
    title: 'After the open',
    when: '09:27',
    duty: 'Is the engine trading: first cycle, first scan, no timeout storms',
  },
  'close-day': {
    title: 'Close',
    when: '15:36',
    duty: 'Books, data, errors, jobs, then the review and the one Telegram line',
  },
}

const CHECK_TITLES: Record<string, string> = {
  'open-day': 'Open: readiness checks',
  liveness: 'After the open: liveness checks',
  logs: 'Close: errors today',
  'value-areas': 'Close: value areas (VAH / POC / VAL)',
  jobs: 'Close: scheduled jobs',
}

function verdictClass(verdict: string) {
  if (verdict === 'GREEN' || verdict === 'OK') return 'bg-green-600 text-white'
  if (verdict === 'RED' || verdict === 'FAIL') return 'bg-red-600 text-white'
  return 'bg-amber-500 text-white'
}

function phaseVerdict(steps: WorkflowStep[] | null) {
  if (steps === null) return { label: 'Not run', className: 'bg-muted text-muted-foreground' }
  if (steps.some((s) => s.exit !== 0))
    return { label: 'Step failed', className: verdictClass('RED') }
  return { label: 'All steps ran', className: verdictClass('GREEN') }
}

function PhaseCard({ phase }: { phase: WorkflowPhase }) {
  const info = PHASE_INFO[phase.phase]
  const state = phaseVerdict(phase.steps)
  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="text-base">
            {info.title} <span className="text-muted-foreground font-normal">{info.when}</span>
          </CardTitle>
          <Badge className={state.className}>{state.label}</Badge>
        </div>
        <p className="text-xs text-muted-foreground">{info.duty}</p>
      </CardHeader>
      <CardContent>
        {phase.steps && phase.steps.length > 0 ? (
          <ul className="space-y-1 text-sm">
            {phase.steps.map((s) => (
              <li key={s.step} className="flex justify-between gap-2">
                <span>{s.step}</span>
                <span className="text-muted-foreground">
                  {s.started}-{s.ended}{' '}
                  <span className={s.exit === 0 ? '' : 'text-red-600 font-semibold'}>
                    {s.exit === 0
                      ? 'ok'
                      : s.exit === 124
                        ? 'stopped at deadline'
                        : `exit ${s.exit}`}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-muted-foreground">No run recorded for this day.</p>
        )}
      </CardContent>
    </Card>
  )
}

function ChecksCard({ name, result }: { name: string; result: WorkflowCheckResult }) {
  const shown = result.checks.filter((c) => c.status !== 'OK')
  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="text-base">{CHECK_TITLES[name] ?? name}</CardTitle>
          <Badge className={verdictClass(result.verdict)}>{result.verdict}</Badge>
        </div>
        <p className="text-xs text-muted-foreground">{result.summary}</p>
      </CardHeader>
      <CardContent>
        {shown.length === 0 ? (
          <p className="text-sm text-muted-foreground">All {result.checks.length} checks OK.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Check</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Evidence</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {shown.map((c) => (
                <TableRow key={`${c.name}-${c.evidence}`}>
                  <TableCell className="whitespace-normal">{c.name}</TableCell>
                  <TableCell>
                    <Badge className={verdictClass(c.status)}>{c.status}</Badge>
                  </TableCell>
                  <TableCell className="whitespace-normal text-xs">{c.evidence}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  )
}

export default function DailyWorkflow() {
  const [day, setDay] = useState<string | undefined>(undefined)
  const [report, setReport] = useState<string | null>(null)
  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ['ops-workflow', day],
    queryFn: () => opsWorkflowApi.get(day),
  })
  const reportQuery = useQuery({
    queryKey: ['ops-report', report],
    queryFn: () => opsWorkflowApi.report(report ?? ''),
    enabled: report !== null,
  })
  const view = data?.data

  return (
    <div className="py-6 space-y-6">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <CalendarCheck className="h-6 w-6" />
            Daily Workflow
          </h1>
          <p className="text-muted-foreground mt-1">
            What ran before the open, after the open and at the close, and what each step found
          </p>
        </div>
        <div className="flex items-center gap-2">
          {data && data.days.length > 0 && (
            <Select
              value={view?.day}
              onValueChange={(value) => {
                setDay(value)
                setReport(null)
              }}
            >
              <SelectTrigger className="w-40">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {data.days.map((d) => (
                  <SelectItem key={d} value={d}>
                    {d}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
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
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {view.phases.map((p) => (
              <PhaseCard key={p.phase} phase={p} />
            ))}
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            {Object.entries(view.checks).map(([name, result]) => (
              <ChecksCard key={name} name={name} result={result} />
            ))}
          </div>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base flex items-center gap-2">
                <FileText className="h-4 w-4" />
                Reports for {view.day}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="flex flex-wrap gap-2">
                {view.reports.map((name) => (
                  <Button
                    key={name}
                    size="sm"
                    variant={report === name ? 'default' : 'outline'}
                    onClick={() => setReport(report === name ? null : name)}
                  >
                    {name.replace(`-${view.day}`, '').replace('.md', '')}
                  </Button>
                ))}
              </div>
              {report !== null &&
                (reportQuery.isLoading ? (
                  <Skeleton className="h-48 w-full" />
                ) : reportQuery.error ? (
                  <p className="text-sm">{(reportQuery.error as Error).message}</p>
                ) : (
                  <pre className="text-xs whitespace-pre-wrap bg-muted/40 p-4 rounded-md max-h-[70vh] overflow-auto">
                    {reportQuery.data}
                  </pre>
                ))}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  )
}
