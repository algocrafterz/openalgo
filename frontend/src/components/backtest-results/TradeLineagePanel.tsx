import { useEffect, useRef } from 'react'
import type { TradeRow } from '@/api/backtestResults'
import { useTradeLineage } from '@/hooks/useTradeLineage'
import { COST_COMPONENT_KEYS } from '@/lib/backtestEvidence'
import { InfoTip } from './InfoTip'

function show(v: unknown): string {
  if (v === null || v === undefined) return 'n/a'
  return typeof v === 'object' ? JSON.stringify(v) : String(v)
}

function Fields({ data }: { data: Record<string, unknown> }) {
  return (
    <dl className="flex flex-wrap gap-x-4 gap-y-0.5 text-xs">
      {Object.entries(data).map(([k, v]) => (
        <div key={k}>
          <dt className="inline text-muted-foreground">{k}: </dt>
          <dd className="inline font-mono">{show(v)}</dd>
        </div>
      ))}
    </dl>
  )
}

function Step({
  step,
  title,
  children,
}: {
  step: string
  title: string
  children: React.ReactNode
}) {
  return (
    <li data-step={step} className="rounded border p-2 space-y-0.5">
      <div className="text-sm font-medium">{title}</div>
      {children}
    </li>
  )
}

type FillRole = 'entry' | 'exit' | 'other'

/** Entry or exit only when the fill says so, by kind or by the trade's own fill ids. */
function fillRole(fill: Record<string, unknown>, row: TradeRow): FillRole {
  if (fill.kind === 'entry') return 'entry'
  if (fill.kind === 'exit') return 'exit'
  const id = fill.fill_id
  if (id !== undefined && id !== null) {
    if (id === row.entry_fill_id) return 'entry'
    if (id === row.exit_fill_id) return 'exit'
  }
  return 'other'
}

function Chain({ ideaId, runId, row }: { ideaId: string; runId: string; row: TradeRow }) {
  const q = useTradeLineage(ideaId, runId, row.trade_id)
  if (q.isLoading) return <p className="text-sm text-muted-foreground">Loading lineage</p>
  const lineage = q.data?.lineage
  if (q.isError || q.data?.status === 'error' || !lineage) {
    return (
      <p role="alert" className="text-sm text-red-600">
        Could not load the lineage for this trade
      </p>
    )
  }
  const fills = lineage.fills ?? []
  const entries = fills.filter((f) => fillRole(f, row) === 'entry')
  const exits = fills.filter((f) => fillRole(f, row) === 'exit')
  const others = fills.filter((f) => fillRole(f, row) === 'other')
  const missing = lineage.missing ?? []
  return (
    <>
      {missing.length > 0 && (
        <p data-testid="lineage-missing" role="note" className="text-sm text-amber-700">
          Not available for this trade: {missing.join(', ')}. The chain below is incomplete.
        </p>
      )}
      <ol className="space-y-2" aria-label="Lineage chain">
        <Step step="signal" title="Signal">
          {lineage.signal ? (
            <Fields data={lineage.signal} />
          ) : (
            <span className="text-xs text-muted-foreground">Not recorded</span>
          )}
        </Step>
        <Step step="order" title="Order">
          {lineage.order ? (
            <Fields data={lineage.order} />
          ) : (
            <span className="text-xs text-muted-foreground">Not recorded</span>
          )}
        </Step>
        <Step step="entry_fill" title="Entry fill">
          {entries.length === 0 ? (
            <span className="text-xs text-muted-foreground">Not recorded</span>
          ) : (
            entries.map((f, i) => <Fields key={`e${i}`} data={f} />)
          )}
        </Step>
        <Step step="exit_fill" title="Exit fill(s)">
          {exits.length === 0 ? (
            <span className="text-xs text-muted-foreground">Not recorded</span>
          ) : (
            exits.map((f, i) => <Fields key={`x${i}`} data={f} />)
          )}
        </Step>
        {others.length > 0 && (
          <li data-testid="other-fills" className="rounded border border-dashed p-2 space-y-0.5">
            <div className="text-sm font-medium">Other fills</div>
            {others.map((f, i) => (
              <Fields key={`o${i}`} data={f} />
            ))}
          </li>
        )}
        <Step step="position" title="Position">
          <span className="font-mono text-xs">{show(lineage.position_id)}</span>
        </Step>
      </ol>
    </>
  )
}

/** One trade's own fields, its cost components, and the signal-to-position chain when recorded. */
export function TradeLineagePanel({
  ideaId,
  runId,
  row,
  hasLedgers,
  onClose,
}: {
  ideaId: string
  runId: string
  row: TradeRow
  hasLedgers: boolean
  onClose: () => void
}) {
  const costs = Object.entries(row).filter(
    ([k, v]) => COST_COMPONENT_KEYS.includes(k) && typeof v === 'number'
  )
  const headingRef = useRef<HTMLHeadingElement>(null)
  useEffect(() => {
    headingRef.current?.focus()
    headingRef.current?.scrollIntoView?.({ block: 'nearest' })
  }, [])
  return (
    <section
      data-testid="lineage-panel"
      aria-label={`Lineage for ${row.trade_id}`}
      className="rounded-md border p-3 space-y-3"
    >
      <div className="flex items-center justify-between">
        <h3 ref={headingRef} tabIndex={-1} className="text-sm font-semibold outline-none">
          Trade {row.trade_id} <InfoTip term="lineage" />
        </h3>
        <button type="button" className="text-xs underline" onClick={onClose}>
          Close lineage
        </button>
      </div>
      <dl className="flex flex-wrap gap-x-5 gap-y-1 text-sm">
        <div>
          <dt className="inline text-muted-foreground">Exit reason: </dt>
          <dd className="inline">{row.exit_reason ?? 'n/a'}</dd>
        </div>
        <div>
          <dt className="inline text-muted-foreground">Entry price: </dt>
          <dd className="inline font-mono">{show(row.entry_price)}</dd>
        </div>
        <div>
          <dt className="inline text-muted-foreground">Exit price: </dt>
          <dd className="inline font-mono">{show(row.exit_price)}</dd>
        </div>
      </dl>
      <div data-testid="trade-costs" className="text-xs">
        <div className="font-medium">Cost components for this trade (bps)</div>
        {costs.length === 0 ? (
          <span className="text-muted-foreground">Not recorded</span>
        ) : (
          <Fields data={Object.fromEntries(costs)} />
        )}
      </div>
      {hasLedgers ? (
        <Chain ideaId={ideaId} runId={runId} row={row} />
      ) : (
        <p className="text-sm text-muted-foreground">
          Lineage ledgers were not recorded for this run
        </p>
      )}
    </section>
  )
}
