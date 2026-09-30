import type { EvaluationBlock, GateRow, GateStatus } from '@/api/backtestResults'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { fmtNum } from '@/lib/backtestResults'
import { cn } from '@/lib/utils'

const STATUS_TEXT: Record<GateStatus, string> = {
  pass: 'pass',
  fail: 'fail',
  report: 'report only',
  not_evaluated: 'not evaluated',
  not_applicable: 'not applicable',
}

const STATUS_CLASS: Record<GateStatus, string> = {
  pass: 'text-green-600',
  fail: 'text-red-600 font-medium',
  report: 'text-foreground',
  not_evaluated: 'text-muted-foreground',
  not_applicable: 'text-muted-foreground',
}

/** The X.2 regime-dispersion bar is this project's own rule of thumb, not a criteria gate. */
const HEURISTIC_GATES = new Set(['X.2'])

const STATUS_ORDER: GateStatus[] = ['pass', 'fail', 'report', 'not_evaluated', 'not_applicable']

function valueText(value: GateRow['value']): string {
  if (value === null || value === undefined) return 'n/a'
  return typeof value === 'number' ? fmtNum(value, 3) : String(value)
}

function dimensionLabel(name: string): string {
  return name.replace(/_/g, ' ')
}

function Counts({ counts }: { counts: EvaluationBlock['summary']['counts'] }) {
  const shown = STATUS_ORDER.filter((s) => (counts[s] ?? 0) > 0)
  if (shown.length === 0) return null
  return (
    <ul className="flex flex-wrap gap-2 text-xs" data-testid="evaluation-counts">
      {shown.map((s) => (
        <li key={s} className="rounded-full border px-2 py-0.5">
          {counts[s]} {STATUS_TEXT[s]}
        </li>
      ))}
    </ul>
  )
}

function DimensionTable({ name, rows }: { name: string; rows: GateRow[] }) {
  return (
    <div className="space-y-1" data-testid={`dimension-${name}`}>
      <div className="text-sm font-medium capitalize">{dimensionLabel(name)}</div>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="w-16">Gate</TableHead>
            <TableHead>Check</TableHead>
            <TableHead>Result</TableHead>
            <TableHead className="text-right">Measured</TableHead>
            <TableHead>Threshold</TableHead>
            <TableHead>Evidence</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row) => {
            const hardFail = row.hard && row.status === 'fail'
            return (
              <TableRow
                key={row.gate}
                data-testid={`gate-${row.gate}`}
                className={cn(hardFail && 'bg-red-500/10')}
              >
                <TableCell className="font-mono text-xs">{row.gate}</TableCell>
                <TableCell>
                  {row.title}
                  {row.hard && <span className="ml-1 text-xs text-muted-foreground">(hard)</span>}
                  {HEURISTIC_GATES.has(row.gate) && (
                    <span className="ml-1 text-xs text-muted-foreground">
                      (heuristic: the bar is a rule of thumb, not yet validated)
                    </span>
                  )}
                </TableCell>
                <TableCell className={cn('text-sm', STATUS_CLASS[row.status])}>
                  {STATUS_TEXT[row.status]}
                </TableCell>
                <TableCell className="text-right font-mono">{valueText(row.value)}</TableCell>
                <TableCell className="text-sm">{row.threshold || 'n/a'}</TableCell>
                <TableCell className="text-sm text-muted-foreground">{row.evidence}</TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    </div>
  )
}

/**
 * Acceptance-criteria gates by dimension. No composite score is shown on purpose: a
 * single number would hide which dimension failed and why.
 */
export function EvaluationTable({ evaluation }: { evaluation: EvaluationBlock }) {
  const failed = evaluation.summary.hard_failed
  return (
    <Card data-testid="evaluation">
      <CardHeader>
        <CardTitle>Evaluation against the acceptance criteria</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <Counts counts={evaluation.summary.counts} />
        {failed.length > 0 ? (
          <div role="alert" className="rounded-md border border-red-500/50 p-3 text-sm">
            Hard gates failed: {failed.join(', ')}. A failed hard gate stops promotion regardless of
            any other result.
          </div>
        ) : (
          <div className="text-sm text-muted-foreground">No hard gate has failed.</div>
        )}
        <p className="text-xs text-muted-foreground">
          Gates that could not be measured are listed as not evaluated, never hidden. Not evaluated
          is not a pass.
        </p>
        {evaluation.dimensions.map((d) => (
          <DimensionTable key={d.name} name={d.name} rows={d.rows} />
        ))}
      </CardContent>
    </Card>
  )
}
