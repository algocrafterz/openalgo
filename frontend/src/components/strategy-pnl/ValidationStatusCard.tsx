import type { ValidationCheck, ValidationState, ValidationStatus } from '@/api/strategyPnl'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { cn } from '@/lib/utils'

const STATE_CLASS: Record<ValidationState, string> = {
  not_enough_data: 'bg-muted text-muted-foreground',
  not_encouraging: 'bg-red-600/15 text-red-600',
  preliminary: 'bg-amber-500/15 text-amber-600',
  under_validation: 'bg-sky-500/15 text-sky-600',
  validated: 'bg-emerald-600/20 text-emerald-600',
}

const DIMENSION_NAMES: Record<string, string> = {
  performance: 'Performance',
  statistical: 'Statistics',
  robustness: 'Robustness',
  execution: 'Execution',
}

const CHECK_MARK: Record<string, { text: string; className: string }> = {
  pass: { text: 'Passed', className: 'text-green-600' },
  fail: { text: 'Failed', className: 'text-red-600' },
  pending: { text: 'Pending', className: 'text-amber-600' },
  not_assessed: { text: 'Not assessed', className: 'text-muted-foreground' },
}

/** An unexpected status from the server reads as not assessed rather than breaking the card. */
function markFor(status: string) {
  return CHECK_MARK[status] ?? CHECK_MARK.not_assessed
}

export function ValidationBadge({
  state,
  label,
  title,
}: {
  state: ValidationState | null | undefined
  label?: string | null
  title?: string | null
}) {
  if (!state) return <span className="text-muted-foreground">—</span>
  return (
    <span
      className={cn(
        'rounded px-1.5 py-0.5 text-xs font-medium whitespace-nowrap',
        STATE_CLASS[state]
      )}
      title={title ?? undefined}
    >
      {label ?? state}
    </span>
  )
}

function CheckList({ title, checks }: { title: string; checks: ValidationCheck[] }) {
  if (checks.length === 0) return null
  return (
    <div className="space-y-1">
      <div className="text-xs font-medium">{title}</div>
      <ul className="space-y-1">
        {checks.map((c) => (
          <li key={c.id} className="text-xs">
            <span className={cn('font-medium', markFor(c.status).className)}>
              {markFor(c.status).text}
            </span>
            {': '}
            {c.label}. <span className="text-muted-foreground">{c.detail}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

/** How far the observed results can be trusted: one status, four evidence dimensions. */
export function ValidationStatusCard({ validation: v }: { validation: ValidationStatus }) {
  const byId = new Map(v.checks.map((c) => [c.id, c]))
  const pick = (ids: string[]) =>
    ids.map((id) => byId.get(id)).filter((c): c is ValidationCheck => c !== undefined)
  const failed = pick(v.failed)
  const waiting = pick(v.waiting)
  const passed = v.checks.filter((c) => c.status === 'pass')
  const blocking = pick(v.blocking)
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-3">
          Validation status <ValidationBadge state={v.status} label={v.label} />
          <span className="text-xs font-normal text-muted-foreground">
            {v.passed_count} of {v.total_count} checks passed
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-sm">{v.summary}</p>
        <div className="flex flex-wrap gap-2 text-xs">
          {Object.entries(v.dimensions).map(([dim, status]) => (
            <span key={dim} className="rounded border px-2 py-0.5">
              {DIMENSION_NAMES[dim] ?? dim}:{' '}
              <span className={cn('font-medium', markFor(status).className)}>
                {markFor(status).text}
              </span>
            </span>
          ))}
        </div>
        {v.next_level_label && blocking.length > 0 && (
          <CheckList title={`Needed to reach "${v.next_level_label}"`} checks={blocking} />
        )}
        <CheckList title="Failed" checks={failed.filter((c) => !blocking.includes(c))} />
        <details className="text-xs">
          <summary className="cursor-pointer text-muted-foreground">
            All {v.total_count} checks
          </summary>
          <div className="mt-2 space-y-2">
            <CheckList title="Passed" checks={passed} />
            <CheckList
              title="Pending or not assessed"
              checks={waiting.filter((c) => !blocking.includes(c))}
            />
          </div>
        </details>
        <p className="text-xs text-muted-foreground">
          This describes how much evidence there is so far. It does not guarantee future results. A
          failed check stops the next level but does not hide the result. Missing evidence is shown
          as not assessed and never counts as a pass.
          {v.mode === 'live'
            ? ''
            : v.mode
              ? ' Sandbox fills are simulated, so live execution cannot be assessed and the status stops at Under validation.'
              : ' The trading mode is not known, so live execution cannot be assessed and the status stops at Under validation.'}
        </p>
      </CardContent>
    </Card>
  )
}
