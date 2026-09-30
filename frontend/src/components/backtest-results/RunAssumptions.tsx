import type { RunRecord } from '@/api/backtestResults'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { tVsHurdle } from '@/lib/backtestResults'

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="font-mono break-all whitespace-pre-wrap min-w-0">{value}</dd>
    </>
  )
}

function formatParam(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value)
}

/** Everything needed to reproduce the run and judge whether it can be compared fairly. */
export function RunAssumptions({ run }: { run: RunRecord }) {
  const params = Object.entries(run.params ?? {})
  return (
    <Card>
      <CardHeader>
        <CardTitle>Assumptions and provenance</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-6 gap-y-1 text-sm">
          <Row label="Universe" value={run.data.universe} />
          <Row label="Test period" value={`${run.period.start} to ${run.period.end}`} />
          <Row label="Bar size" value={run.data.bar_size} />
          <Row label="Data source" value={run.data.source} />
          <Row label="Data as of" value={run.data.as_of} />
          <Row label="Cost model" value={run.costs.model} />
          <Row label="Round-trip cost" value={`${run.costs.round_trip_bps} bps`} />
          <Row label="Code version" value={run.code_version} />
          <Row label="Variants tried" value={run.n_trials ?? 1} />
          <Row label="t-statistic vs hurdle" value={tVsHurdle(run.metrics)} />
        </dl>
        <div>
          <div className="text-sm font-medium mb-1">Parameters</div>
          {params.length === 0 ? (
            <div className="text-sm text-muted-foreground">None recorded</div>
          ) : (
            <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-6 gap-y-1 text-sm">
              {params.map(([k, v]) => (
                <Row key={k} label={k} value={formatParam(v)} />
              ))}
            </dl>
          )}
        </div>
        {run.validation && run.validation.length > 0 && (
          <div>
            <div className="text-sm font-medium mb-1">Validation</div>
            <ul className="list-disc pl-5 text-sm">
              {run.validation.map((v) => (
                <li key={v}>{v}</li>
              ))}
            </ul>
          </div>
        )}
        {run.notes && <p className="text-sm text-muted-foreground">{run.notes}</p>}
      </CardContent>
    </Card>
  )
}
