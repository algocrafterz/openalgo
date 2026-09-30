import type { StrategySpec } from '@/api/backtestResults'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

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

/**
 * The current canonical rules, from the idea's spec.yaml. History, rejected variants and
 * observations are kept out of it on purpose; they live in Research notes.
 */
export function StrategyDefinition({ spec }: { spec: StrategySpec | null | undefined }) {
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
        <dl className="grid gap-3 md:grid-cols-3">
          {CONTEXT.map((f) => (
            <FieldView key={f.key} spec={spec} field={f} />
          ))}
        </dl>
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
        {params.length > 0 && (
          <div className="text-sm font-mono">
            {params.map(([k, v]) => (
              <div key={k}>
                {k} = {typeof v === 'string' ? v : JSON.stringify(v)}
              </div>
            ))}
          </div>
        )}
        {asList(spec.rationale).length > 0 && (
          <p className="text-sm text-muted-foreground">Rationale: {asList(spec.rationale)[0]}</p>
        )}
      </CardContent>
    </Card>
  )
}
