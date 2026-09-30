import type { IdeaCard, Lineage, RunRecord } from '@/api/backtestResults'
import { buildVerdict, type VerdictTone } from '@/lib/backtestResults'
import { cn } from '@/lib/utils'

const TONE_CLASS: Record<VerdictTone, string> = {
  fail: 'border-red-500/60 bg-red-500/5',
  warn: 'border-amber-500/60 bg-amber-500/5',
  ok: 'border-green-500/60 bg-green-500/5',
  unknown: 'border-border',
}

/**
 * The first thing on the page: is there evidence of an edge, what blocks it, and is more
 * tuning worthwhile. Everything below it is the evidence for this paragraph.
 */
export function VerdictBanner({
  idea,
  run,
  lineage,
}: {
  idea: Pick<IdeaCard, 'verdict' | 'status' | 'rejection_basis'>
  run: RunRecord | undefined
  lineage: Lineage | null | undefined
}) {
  const v = buildVerdict(idea, run, lineage)
  return (
    <section
      className={cn('rounded-md border p-4 space-y-2 max-w-3xl', TONE_CLASS[v.tone])}
      data-testid="verdict-banner"
      aria-label="Verdict"
    >
      <h2 className="text-lg font-semibold" data-testid="verdict-headline">
        {v.headline}
      </h2>
      {v.points.length > 0 && (
        <ul className="list-disc pl-5 text-sm space-y-0.5" data-testid="lineage">
          {v.points.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}
    </section>
  )
}
