import type { RunFingerprints, StrategySpec } from '@/api/backtestResults'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { ShortHash } from './ShortHash'

/** A label and the spec keys that can hold it: the current schema key first, legacy keys after. */
type Field = { keys: string[]; label: string }

const RULES: Field[] = [
  { keys: ['entry'], label: 'Entry' },
  { keys: ['exit'], label: 'Exit' },
  { keys: ['stop', 'stop_loss'], label: 'Stop loss' },
  { keys: ['take_profit'], label: 'Take profit' },
  { keys: ['sizing', 'position_sizing'], label: 'Position sizing' },
  { keys: ['holding_period', 'max_holding_period'], label: 'Holding period' },
  { keys: ['re_entry'], label: 'Re-entry' },
  { keys: ['timing'], label: 'Timing' },
  { keys: ['filters'], label: 'Filters' },
  { keys: ['session_window', 'session_filters'], label: 'Session' },
]

const CONTEXT: Field[] = [
  { keys: ['market'], label: 'Market' },
  { keys: ['timeframe'], label: 'Timeframe' },
  { keys: ['style', 'trading_style'], label: 'Style' },
  { keys: ['direction'], label: 'Direction' },
  { keys: ['cost_assumptions'], label: 'Cost assumptions' },
  { keys: ['constraints'], label: 'Constraints' },
]

function scalar(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value)
}

/** One readable line for a value: text as is, a structured object as `name (key value, ...)`. */
function describe(value: unknown): string {
  if (value === null || typeof value !== 'object') return String(value)
  if (Array.isArray(value)) return value.map(describe).join(', ')
  const entries = Object.entries(value as Record<string, unknown>)
  const name = entries.find(([k]) => k === 'name')
  const rest = entries
    .filter(([k]) => k !== 'name')
    .map(([k, v]) => `${k.replace(/_/g, ' ')} ${typeof v === 'object' ? describe(v) : scalar(v)}`)
  if (name) return rest.length ? `${String(name[1])} (${rest.join(', ')})` : String(name[1])
  return rest.join(', ')
}

function asList(value: unknown): string[] {
  if (value === undefined || value === null) return []
  return (Array.isArray(value) ? value : [value]).map(describe).filter((line) => line !== '')
}

function pick(spec: StrategySpec, keys: string[]): unknown {
  const record = spec as Record<string, unknown>
  const key = keys.find((k) => record[k] !== undefined && record[k] !== null)
  return key === undefined ? undefined : record[key]
}

function FieldView({ spec, field }: { spec: StrategySpec; field: Field }) {
  const items = asList(pick(spec, field.keys))
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
            <FieldView key={f.keys[0]} spec={spec} field={f} />
          ))}
        </dl>
        <section data-testid="spec-rules" className="space-y-2">
          <h4 className="text-sm font-medium">Rules</h4>
          <dl className="grid gap-3 md:grid-cols-2">
            {RULES.map((f) => (
              <FieldView key={f.keys[0]} spec={spec} field={f} />
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
