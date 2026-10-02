import { useState } from 'react'
import type {
  EvaluationBlock,
  GateRow,
  Promotion,
  PromotionDecision,
  PromotionGateStatus,
} from '@/api/backtestResults'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { findGateRow, fmtNum } from '@/lib/backtestResults'
import { cn } from '@/lib/utils'
import { InfoTip } from './InfoTip'

export const DECISION_TEXT: Record<PromotionDecision, string> = {
  READY_FOR_NEXT_STAGE: 'Ready for the next stage',
  NOT_READY: 'Not ready',
  BLOCKED: 'Blocked',
}

export const DECISION_CLASS: Record<PromotionDecision, string> = {
  READY_FOR_NEXT_STAGE: 'text-green-600',
  NOT_READY: 'text-amber-600',
  BLOCKED: 'text-muted-foreground',
}

const UNKNOWN_CLASS = 'text-muted-foreground'

/** An unknown or absent decision shows as its raw string, muted, never blank. */
export function decisionText(decision: string | null | undefined): string {
  if (!decision) return 'Not available'
  return (DECISION_TEXT as Record<string, string>)[decision] ?? String(decision)
}

export function decisionClass(decision: string | null | undefined): string {
  return (decision && (DECISION_CLASS as Record<string, string>)[decision]) || UNKNOWN_CLASS
}

const STATUS_TEXT: Record<PromotionGateStatus, string> = {
  PASS: 'Pass',
  FAIL: 'Fail',
  NOT_RUN: 'Not run',
  NOT_APPLICABLE: 'Not applicable',
  REPORT: 'Report only',
}

/** NOT_RUN is deliberately dashed and muted: absent evidence must never look like a pass. */
const STATUS_CLASS: Record<PromotionGateStatus, string> = {
  PASS: 'text-green-600 border-green-600/50',
  FAIL: 'text-red-600 border-red-600/60 font-medium',
  NOT_RUN: 'text-muted-foreground border-dashed border-muted-foreground/50 italic',
  NOT_APPLICABLE: 'text-muted-foreground border-transparent',
  REPORT: 'text-foreground border-border',
}

const STAGES = [
  { key: 'backtest', label: 'Backtest', range: 'G0-G6' },
  { key: 'paper', label: 'Paper', range: 'G7-G8' },
  { key: 'live', label: 'Live', range: 'G9' },
] as const

const EXPANDABLE: PromotionGateStatus[] = ['FAIL', 'NOT_RUN']

function valueText(value: GateRow['value']): string {
  if (value === null || value === undefined) return 'n/a'
  return typeof value === 'number' ? fmtNum(value, 3) : String(value)
}

function GateItem({
  gate,
  status,
  row,
}: {
  gate: string
  status: PromotionGateStatus
  row: GateRow | undefined
}) {
  const [open, setOpen] = useState(false)
  const expandable = EXPANDABLE.includes(status)
  const badge = (
    <span
      data-status={status}
      className={cn(
        'rounded border px-1.5 py-0.5 text-xs',
        STATUS_CLASS[status] ?? `${UNKNOWN_CLASS} border-border`
      )}
    >
      {STATUS_TEXT[status] ?? String(status)}
    </span>
  )
  return (
    <li className="py-1 text-sm" data-testid={`promotion-gate-${gate}`}>
      <div className="flex items-center gap-2">
        <span className="font-mono text-xs w-14">{gate}</span>
        {badge}
        {row && <span className="text-muted-foreground">{row.title}</span>}
        {expandable && (
          <button
            type="button"
            className="ml-auto text-xs underline"
            aria-expanded={open}
            onClick={() => setOpen((o) => !o)}
          >
            {open ? 'Hide evidence' : 'Show evidence'}
          </button>
        )}
      </div>
      {expandable && open && (
        <dl className="mt-1 ml-16 space-y-0.5 text-xs rounded border p-2">
          {row ? (
            <>
              <div>
                <dt className="inline font-medium">Check: </dt>
                <dd className="inline">{row.title}</dd>
              </div>
              <div>
                <dt className="inline font-medium">Threshold: </dt>
                <dd className="inline">{row.threshold || 'n/a'}</dd>
              </div>
              <div>
                <dt className="inline font-medium">Measured: </dt>
                <dd className="inline font-mono">{valueText(row.value)}</dd>
              </div>
              <div>
                <dt className="inline font-medium">Evidence: </dt>
                <dd className="inline text-muted-foreground">{row.evidence || 'n/a'}</dd>
              </div>
            </>
          ) : (
            <div className="text-muted-foreground">No evaluation row for this gate.</div>
          )}
        </dl>
      )}
    </li>
  )
}

function count(gates: { status: string }[], s: string): number {
  return gates.filter((g) => g.status === s).length
}

/** Evaluation gates grouped by the stage they decide, with what is missing one click away. */
export function PromotionGate({
  promotion,
  evaluation,
}: {
  promotion: Promotion | null
  evaluation: EvaluationBlock | null
}) {
  if (!promotion) return null
  return (
    <Card data-testid="promotion-gate" className="max-w-3xl">
      <CardHeader>
        <CardTitle className="text-base">
          Promotion gates <InfoTip term="promotion_gate" />
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {STAGES.map(({ key, label, range }) => {
          const st = promotion.stages?.[key]
          const gates = st?.gates ?? []
          return (
            <section key={key} data-testid={`gate-stage-${key}`} className="space-y-1">
              <div className="flex flex-wrap items-baseline gap-2">
                <h3 className="text-sm font-semibold">{label}</h3>
                <span className="text-xs text-muted-foreground">{range}</span>
                <span className={cn('text-sm', decisionClass(st?.decision))}>
                  {decisionText(st?.decision)}
                </span>
                <span className="text-xs text-muted-foreground">
                  {count(gates, 'PASS')} pass, {count(gates, 'FAIL')} fail,{' '}
                  {count(gates, 'NOT_RUN')} not run
                </span>
              </div>
              <ul className="divide-y">
                {gates.map((g, i) => (
                  <GateItem
                    key={`${g.gate}-${i}`}
                    gate={g.gate}
                    status={g.status}
                    row={findGateRow(evaluation, g.gate)}
                  />
                ))}
              </ul>
            </section>
          )
        })}
      </CardContent>
    </Card>
  )
}
