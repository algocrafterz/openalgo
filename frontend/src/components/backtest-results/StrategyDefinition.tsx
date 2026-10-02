import type { RunFingerprints, StrategySpec } from '@/api/backtestResults'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { ShortHash } from './ShortHash'

type Field = { key: keyof StrategySpec; label: string }

const RULES: Field[] = [
  { key: 'entry', label: 'Entry' },
  { key: 'exit', label: 'Exit' },
  { key: 'stop_loss', label: 'Stop loss' },
  { key: 'take_profit', label: 'Take profit' },
  { key: 'position_sizing', label: 'Position sizing' },
  { key: 'max_holding_period', label: 'Maximum holding period' },
  { key: 'filters', label: 'Filters' },
  { key: 'session_filters', label: 'Session filters' },
]

const CONTEXT: Field[] = [
  { key: 'market', label: 'Market' },
  { key: 'timeframe', label: 'Timeframe' },
  { key: 'trading_style', label: 'Style' },
  { key: 'direction', label: 'Direction' },
  { key: 'cost_assumptions', label: 'Cost assumptions' },
  { key: 'constraints', label: 'Constraints' },
]

function asList(value: unknown): string[] {
  if (value === undefined || value === null) return []
  return (Array.isArray(value) ? value : [value]).map(String)
}

function FieldView({ spec, field }: { spec: StrategySpec; field: Field }) {
  const items = asList(spec[field.key])
  if (items.length === 0) return null
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{field.label}</dt>
      <dd className="text-sm">
        {items.length === 1 ? (
          items[0]
        ) : (
          <ul className="list-disc pl-5">
            {items.map((i) => (
              <li key={i}>{i}</li>
            ))}
          </ul>
        )}
      </dd>
    </div>
  )
}

export interface FrozenSpec {
  spec: Record<string, unknown> | null
  version: string | null
  parentVersion: string | null
  branchLabel: string | null
  fingerprints: RunFingerprints | null
}

function FrozenMeta({ frozen }: { frozen: FrozenSpec }) {
  const fp = frozen.fingerprints
  return (
    <div data-testid="frozen-meta" className="text-sm space-y-1">
      <p className="text-xs text-muted-foreground">Frozen with this run</p>
      <div className="flex flex-wrap gap-x-5 gap-y-1">
        <span>Version {frozen.version ?? 'not captured'}</span>
        <span>Parent {frozen.parentVersion ?? 'none'}</span>
        <span>Branch {frozen.branchLabel ?? 'none'}</span>
      </div>
      {fp && (
        <div className="flex flex-wrap gap-x-5 gap-y-1">
          <span>
            Rules <ShortHash value={fp.rules} />
          </span>
          <span>
            Parameters <ShortHash value={fp.params} />
          </span>
          <span>
            Strategy <ShortHash value={fp.strategy} />
          </span>
        </div>
      )}
    </div>
  )
}

/**
 * The strategy rules. When `frozen.spec` is given that is the spec the run was made from and it
 * is shown instead of the idea's current spec.yaml. History, rejected variants and observations
 * are kept out of it on purpose; they live in Research notes.
 */
export function StrategyDefinition({
  spec: currentSpec,
  frozen,
  loading,
}: {
  spec: StrategySpec | null | undefined
  /** Present when the selected run's detail is captured; its `spec` may still be null. */
  frozen?: FrozenSpec
  /** The selected run's detail is still loading: show neither spec yet. */
  loading?: boolean
}) {
  if (loading) {
    return <Skeleton className="h-40 w-full max-w-3xl" data-testid="strategy-definition-loading" />
  }
  if (frozen && !frozen.spec) {
    return (
      <p className="text-sm text-muted-foreground max-w-3xl" data-testid="frozen-spec-missing">
        Frozen spec not captured for this run
      </p>
    )
  }
  const isFrozen = !!frozen?.spec
  const spec = (isFrozen ? frozen?.spec : currentSpec) as StrategySpec | null | undefined
  if (!spec) {
    return (
      <p className="text-sm text-muted-foreground max-w-3xl" data-testid="no-spec">
        No specification on file. The rules exist only in the research notes, which may mix current
        rules with history.
      </p>
    )
  }
  const params = Object.entries(spec.parameters ?? {})
  const indicators = asList(spec.indicators)
  return (
    <Card data-testid="strategy-definition">
      <CardHeader>
        <CardTitle>Strategy definition{spec.name ? `: ${spec.name}` : ''}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {isFrozen && frozen ? (
          <FrozenMeta frozen={frozen} />
        ) : (
          <p className="text-xs text-muted-foreground" data-testid="current-spec-label">
            Current idea spec, not necessarily the one this run used
          </p>
        )}
        <dl className="grid gap-3 md:grid-cols-3">
          {CONTEXT.map((f) => (
            <FieldView key={f.key} spec={spec} field={f} />
          ))}
        </dl>
        <section data-testid="spec-rules" className="space-y-2">
          <h4 className="text-sm font-medium">Rules</h4>
          <dl className="grid gap-3 md:grid-cols-2">
            {RULES.map((f) => (
              <FieldView key={f.key} spec={spec} field={f} />
            ))}
          </dl>
          {indicators.length > 0 && (
            <div className="text-sm">
              <span className="text-xs text-muted-foreground">Indicators: </span>
              {indicators.join(', ')}
            </div>
          )}
        </section>
        {params.length > 0 && (
          <section data-testid="spec-parameters" className="space-y-1">
            <h4 className="text-sm font-medium">Parameters</h4>
            <div className="text-sm font-mono">
              {params.map(([k, v]) => (
                <div key={k}>
                  {k} = {typeof v === 'string' ? v : JSON.stringify(v)}
                </div>
              ))}
            </div>
          </section>
        )}
        {asList(spec.rationale).length > 0 && (
          <p className="text-sm text-muted-foreground">Rationale: {asList(spec.rationale)[0]}</p>
        )}
      </CardContent>
    </Card>
  )
}
