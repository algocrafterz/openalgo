import type { RunScope } from '@/api/backtestResults'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { InfoTip } from './InfoTip'
import { ShortHash } from './ShortHash'

const NOT_CAPTURED = 'not captured'

function show(value: string | number | null | undefined, suffix = ''): string {
  if (value === null || value === undefined || value === '') return NOT_CAPTURED
  return `${value}${suffix}`
}

function windowText(w: RunScope['strategy_window']): string {
  if (w === null || w === undefined) return NOT_CAPTURED
  if (typeof w === 'string') return w
  const parts: string[] = []
  if (w.entry) parts.push(`entry ${w.entry}`)
  if (w.no_trade && w.no_trade.length > 0) parts.push(`no trade ${w.no_trade.join(', ')}`)
  return parts.length > 0 ? parts.join('; ') : NOT_CAPTURED
}

function Row({
  label,
  value,
  testId,
  term,
}: {
  label: string
  value: React.ReactNode
  testId?: string
  term?: string
}) {
  const missing = value === NOT_CAPTURED
  return (
    <div data-testid={testId} className="flex gap-3 text-sm">
      <dt className="w-44 shrink-0 text-muted-foreground">
        {label}
        {term && <InfoTip term={term} />}
      </dt>
      <dd className={`min-w-0 break-words ${missing ? 'text-muted-foreground italic' : ''}`}>
        {value}
      </dd>
    </div>
  )
}

function Group({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-1">
      <h4 className="text-sm font-medium">{title}</h4>
      <dl className="space-y-1">{children}</dl>
    </section>
  )
}

/** What this run covered, generated entirely from the frozen configuration. */
export function BacktestScope({ scope }: { scope: RunScope | null }) {
  return (
    <Card data-testid="backtest-scope">
      <CardHeader>
        <CardTitle>Backtest scope</CardTitle>
      </CardHeader>
      <CardContent>
        {scope === null ? (
          <p className="text-sm text-muted-foreground">Not captured for this run</p>
        ) : (
          <div className="grid gap-5 md:grid-cols-2">
            <Group title="Market and universe">
              <Row label="Market" value={show(scope.market)} />
              <Row label="Universe" value={show(scope.universe)} />
              <Row label="Symbol count" value={show(scope.symbol_count)} />
            </Group>
            <Group title="Strategy">
              <Row label="Style" value={show(scope.style)} />
              <Row label="Direction" value={show(scope.direction)} />
              <Row label="Timeframe" value={show(scope.timeframe)} />
              <Row label="Product" value={show(scope.product)} />
              <Row label="Portfolio mode" value={show(scope.portfolio_mode)} />
              <Row label="Portfolio scope" value={show(scope.portfolio_scope)} />
              <Row label="Capital per trade" value={show(scope.capital_per_trade)} />
              <Row label="Risk per trade" value={show(scope.risk_per_trade_pct, ' %')} />
            </Group>
            <Group title="Period">
              <Row
                label="Test period"
                value={scope.period ? `${scope.period.start} to ${scope.period.end}` : NOT_CAPTURED}
              />
            </Group>
            <Group title="Sessions">
              <Row
                label="Strategy window"
                testId="scope-strategy-window"
                value={windowText(scope.strategy_window)}
              />
              <Row
                label="Exchange session"
                testId="scope-exchange-session"
                value={show(scope.exchange_session)}
              />
              <Row
                label="Broker cutoff"
                testId="scope-broker-cutoff"
                value={show(scope.broker_cutoff)}
              />
              <Row label="Force flat" testId="scope-force-flat" value={show(scope.force_flat)} />
            </Group>
            <Group title="Execution">
              <Row
                label="Slippage per side"
                term="slippage"
                value={show(scope.slippage_bps_per_side, ' bps')}
              />
              <Row
                label="Round-trip cost"
                term="transaction_costs"
                value={show(scope.cost_bps_round_trip, ' bps')}
              />
            </Group>
            <Group title="Deployment profile">
              <Row
                label="Name"
                testId="scope-profile-name"
                value={show(scope.profile?.name ?? null)}
              />
              <Row
                label="Hash"
                value={scope.profile ? <ShortHash value={scope.profile.sha256} /> : NOT_CAPTURED}
              />
            </Group>
            <Group title="Dataset">
              <Row label="Id" testId="scope-dataset-id" value={show(scope.dataset?.id ?? null)} />
              <Row label="Source" value={show(scope.dataset?.source ?? null)} />
              <Row label="Interval" value={show(scope.dataset?.interval ?? null)} />
              <Row label="Data as of" value={show(scope.dataset?.data_as_of ?? null)} />
              <Row
                label="Fingerprint"
                value={
                  scope.dataset ? <ShortHash value={scope.dataset.fingerprint} /> : NOT_CAPTURED
                }
              />
            </Group>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
