import type { RunRecord } from '@/api/backtestResults'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { coverageNotes, fmtAge, fmtSpan } from '@/lib/backtestResults'

function Item({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="font-mono text-sm">{children}</dd>
    </div>
  )
}

/**
 * How much market this result saw, and how stale it is. Sits above the numbers so the
 * reader knows the window before reading any figure from it.
 */
export function DataCoverage({ run }: { run: RunRecord }) {
  const notes = coverageNotes({ years: run.years, data_age_days: run.data_age_days })
  const days = run.metrics.days
  const window = [
    `${run.period.start} to ${run.period.end}`,
    ` (${fmtSpan(run.years)}${days === undefined ? '' : `; trades entered on ${days} sessions`})`,
  ].join('')
  return (
    <Card data-testid="data-coverage">
      <CardHeader>
        <CardTitle>Backtest data</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <Item label="Tested window">{window}</Item>
          <Item label="Data snapshot">{`${run.data.as_of} (${fmtAge(run.data_age_days)})`}</Item>
          <Item label="Bars">{run.data.bar_size}</Item>
          <Item label="Universe">{run.data.universe}</Item>
          <Item label="Costs">{`${run.costs.round_trip_bps} bps round trip (${run.costs.model})`}</Item>
        </dl>
        {notes.map((note) => (
          <div
            key={note}
            role="note"
            className="rounded-md border border-yellow-500/50 p-3 text-sm"
          >
            {note}
          </div>
        ))}
      </CardContent>
    </Card>
  )
}
