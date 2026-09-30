import type { RunRecord } from '@/api/backtestResults'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { isKnown, metricText, signClass } from '@/lib/backtestResults'
import { cn } from '@/lib/utils'

interface RunHistoryProps {
  runs: RunRecord[]
  selectedId: string
  onSelect: (runId: string) => void
}

/** Every recorded run, newest first, so tuning progress reads top to bottom. */
export function RunHistory({ runs, selectedId, onSelect }: RunHistoryProps) {
  const ordered = [...runs].reverse()
  return (
    <Card>
      <CardHeader>
        <CardTitle>Run history ({runs.length})</CardTitle>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Date</TableHead>
              <TableHead>Run</TableHead>
              <TableHead className="text-right">Net bps</TableHead>
              <TableHead className="text-right">Trades</TableHead>
              <TableHead className="text-right">Variants tried</TableHead>
              <TableHead>Changed from</TableHead>
              <TableHead>What changed</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {ordered.map((run) => {
              const selected = run.run_id === selectedId
              return (
                <TableRow key={run.run_id} data-state={selected ? 'selected' : undefined}>
                  <TableCell>
                    <button
                      type="button"
                      className={cn(
                        'font-mono underline-offset-4 hover:underline',
                        selected && 'font-bold'
                      )}
                      aria-current={selected ? 'true' : undefined}
                      onClick={() => onSelect(run.run_id)}
                    >
                      {run.date}
                    </button>
                  </TableCell>
                  <TableCell className="font-mono text-xs">
                    {run.run_id}
                    {run.promoted && (
                      <span className="ml-2 font-sans">(promoted to forward test)</span>
                    )}
                  </TableCell>
                  <TableCell
                    className={cn(
                      'text-right font-mono',
                      signClass(run.metrics.net_bps, isKnown(run.metrics, run.metrics.net_bps))
                    )}
                  >
                    {metricText(run.metrics, run.metrics.net_bps)}
                  </TableCell>
                  <TableCell className="text-right font-mono">{run.metrics.trades}</TableCell>
                  <TableCell className="text-right font-mono">{run.n_trials ?? 1}</TableCell>
                  <TableCell className="font-mono text-xs">
                    {run.parent_run_id ?? 'first run'}
                  </TableCell>
                  <TableCell className="text-sm">{run.change_note || 'n/a'}</TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  )
}
