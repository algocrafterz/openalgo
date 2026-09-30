import type { RunRecord, SplitMetrics } from '@/api/backtestResults'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { fmtNum, fmtPercent, isKnown, metricText, signClass } from '@/lib/backtestResults'
import { cn } from '@/lib/utils'

function SplitColumn({ title, m }: { title: string; m: SplitMetrics }) {
  return (
    <div className="rounded-md border p-3 space-y-1">
      <div className="text-sm font-medium">{title}</div>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
        <dt className="text-muted-foreground">Trades</dt>
        <dd className="font-mono text-right">{m.trades}</dd>
        <dt className="text-muted-foreground">Net bps</dt>
        <dd className={cn('font-mono text-right', signClass(m.net_bps, isKnown(m, m.net_bps)))}>
          {metricText(m, m.net_bps)}
        </dd>
        <dt className="text-muted-foreground">Gross bps</dt>
        <dd className="font-mono text-right">{metricText(m, m.gross_bps)}</dd>
        <dt className="text-muted-foreground">Win rate</dt>
        <dd className="font-mono text-right">
          {isKnown(m, m.win_rate) ? fmtPercent(m.win_rate) : 'n/a'}
        </dd>
        <dt className="text-muted-foreground">t-statistic</dt>
        <dd className="font-mono text-right">{metricText(m, m.t_stat, (v) => fmtNum(v))}</dd>
      </dl>
    </div>
  )
}

function SplitPanel({ run }: { run: RunRecord }) {
  if (!run.split || !run.in_sample || !run.out_of_sample) {
    return (
      <div className="text-sm text-muted-foreground">
        No in-sample / out-of-sample split was recorded for this run, so there is no evidence here
        about how the result holds up on data the rules were not tuned on.
      </div>
    )
  }
  return (
    <div className="space-y-2">
      <div className="text-xs text-muted-foreground">
        Out-of-sample starts {run.split.oos_start}
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        <SplitColumn title="In-sample" m={run.in_sample} />
        <SplitColumn title="Out-of-sample" m={run.out_of_sample} />
      </div>
    </div>
  )
}

function CostTable({ run }: { run: RunRecord }) {
  const rows = run.cost_sensitivity ?? []
  if (rows.length === 0) {
    return <div className="text-sm text-muted-foreground">No cost sensitivity recorded</div>
  }
  const known = run.metrics.trades > 0
  return (
    <div className="space-y-2">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Round-trip cost (bps)</TableHead>
            <TableHead className="text-right">Net bps per trade</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((r) => (
            <TableRow key={r.round_trip_bps}>
              <TableCell className="font-mono">
                {r.round_trip_bps}
                {r.round_trip_bps === run.costs.round_trip_bps ? ' (modelled)' : ''}
              </TableCell>
              <TableCell className={cn('text-right font-mono', signClass(r.net_bps, known))}>
                {metricText(run.metrics, r.net_bps)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <div className="text-sm">
        Break-even cost:{' '}
        <span className="font-mono">
          {metricText(run.metrics, run.metrics.breakeven_cost_bps, (v) => `${fmtNum(v)} bps`)}
        </span>{' '}
        <span className="text-muted-foreground">
          (the round-trip cost at which the average trade earns nothing)
        </span>
      </div>
    </div>
  )
}

export function RunRobustness({ run }: { run: RunRecord }) {
  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>In-sample vs out-of-sample</CardTitle>
        </CardHeader>
        <CardContent>
          <SplitPanel run={run} />
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Cost sensitivity</CardTitle>
        </CardHeader>
        <CardContent>
          <CostTable run={run} />
        </CardContent>
      </Card>
    </div>
  )
}
