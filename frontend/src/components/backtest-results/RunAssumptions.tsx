import type { AssumptionEntry, RunRecord } from '@/api/backtestResults'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { tVsHurdle } from '@/lib/backtestResults'
import { InfoTip } from './InfoTip'

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

type ResolvedAssumptions = { sealed_ist: string; entries: AssumptionEntry[] }

const GROUPS: { key: string; title: string; sources: AssumptionEntry['source'][] }[] = [
  { key: 'user', title: 'Stated by you', sources: ['user'] },
  { key: 'derived', title: 'Derived', sources: ['derived'] },
  { key: 'platform_default', title: 'Platform defaults', sources: ['platform_default'] },
  { key: 'external', title: 'External', sources: ['market_data', 'broker_config'] },
  { key: 'inferred', title: 'Inferred (needs your review)', sources: ['inferred'] },
]

function isUnresolved(e: AssumptionEntry): boolean {
  return e.source === 'inferred' && e.materiality === 'high' && !e.user_confirmed
}

function ResolvedView({ assumptions }: { assumptions: ResolvedAssumptions }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          Resolved assumptions
          <InfoTip term="ledger" />
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted-foreground" data-testid="assumptions-sealed">
          Sealed {assumptions.sealed_ist} IST
        </p>
        {GROUPS.map((g) => {
          const entries = assumptions.entries.filter((e) => g.sources.includes(e.source))
          if (entries.length === 0) return null
          return (
            <section key={g.key} data-testid={`assumption-group-${g.key}`}>
              <h4 className="text-sm font-medium mb-1">{g.title}</h4>
              <ul className="space-y-2">
                {entries.map((e) => (
                  <li
                    key={e.id}
                    data-testid={`assumption-${e.id}`}
                    className={`text-sm rounded border p-2 ${isUnresolved(e) ? 'border-destructive' : ''}`}
                  >
                    <div className="flex flex-wrap gap-x-3">
                      <span className="font-medium">{e.field}</span>
                      <span className="font-mono break-all">{formatParam(e.value)}</span>
                      {isUnresolved(e) && <span className="font-medium">Unresolved</span>}
                    </div>
                    <div className="text-muted-foreground">{e.reason}</div>
                    <div className="text-xs text-muted-foreground">
                      Confidence {e.confidence}; materiality {e.materiality};{' '}
                      {e.user_confirmed ? 'Confirmed' : 'Not confirmed'}
                    </div>
                  </li>
                ))}
              </ul>
            </section>
          )
        })}
      </CardContent>
    </Card>
  )
}

/** Everything needed to reproduce the run and judge whether it can be compared fairly. */
export function RunAssumptions({
  run,
  assumptions,
}: {
  run: RunRecord
  assumptions?: ResolvedAssumptions | null
}) {
  if (assumptions) return <ResolvedView assumptions={assumptions} />
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
