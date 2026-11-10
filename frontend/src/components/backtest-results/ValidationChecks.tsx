import { cn } from '@/lib/utils'
import { type CheckGroup, type CheckState, checkStateLabel } from '@/lib/validationChecks'

/** Colour supports the word, it never replaces it. Not run is muted and dashed: never a pass. */
const STATE_CLASS: Record<CheckState, string> = {
  failed: 'border-red-600/60 text-red-600 font-medium',
  not_run: 'border-dashed border-muted-foreground/50 text-muted-foreground italic',
  passed: 'border-green-600/50 text-green-600',
  reported: 'border-border text-foreground',
  not_applicable: 'border-transparent text-muted-foreground',
}

/**
 * The checks behind the answer, in about six questions. Each line is the worst recorded gate
 * result in its group. Every individual gate result is in the Audit section below, unchanged.
 */
export function ValidationChecks({ groups }: { groups: CheckGroup[] }) {
  if (groups.length === 0) return null
  return (
    <section className="space-y-2" data-testid="validation-checks" aria-label="Checks">
      <h2 className="text-sm font-semibold text-muted-foreground">What was checked</h2>
      <ul className="divide-y rounded-md border">
        {groups.map((g) => (
          <li
            key={g.id}
            className="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-3 py-2 text-sm"
            data-testid={`check-${g.id}`}
          >
            <span className="w-56 font-medium">{g.label}</span>
            <span
              className={cn('rounded border px-1.5 py-0.5 text-xs', STATE_CLASS[g.state])}
              data-testid={`check-state-${g.id}`}
              data-state={g.state}
            >
              {checkStateLabel(g.state)}
            </span>
            <span className="min-w-0 flex-1 text-muted-foreground">
              {g.summary}
              {g.decidesVerdict && ' This test decides the verdict.'}
            </span>
          </li>
        ))}
      </ul>
      <p className="text-xs text-muted-foreground">
        Every individual gate result is under Audit below. Paper and live readiness is listed
        separately, because it is not part of a backtest.
      </p>
    </section>
  )
}
