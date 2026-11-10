import type { RunVerdictValue, Verdict } from '@/api/backtestResults'
import { cn } from '@/lib/utils'

export interface PlainVerdictText {
  label: string
  /** One sentence a trader can act on. It describes the test result, not a live-trading go-ahead. */
  sentence: string
  tone: 'good' | 'warn' | 'bad' | 'neutral'
}

/**
 * Every verdict in plain words. The verdict is the calculated result of one backtest run; it is
 * separate from the research stage and from any readiness decision, which are shown elsewhere.
 */
export const PLAIN_VERDICT: Record<Verdict, PlainVerdictText> = {
  edge: {
    label: 'Promising',
    sentence:
      'Made money after costs and passed the required tests in this historical run. It is ready for a forward check in a Sandbox run, which is not yet proof.',
    tone: 'good',
  },
  marginal: {
    label: 'Borderline',
    sentence:
      'Showed some profit after costs, but at least one required test failed or was weak. Not convincing yet.',
    tone: 'warn',
  },
  inconclusive: {
    label: 'Needs more data',
    sentence:
      'The run could not settle the question: there was a problem with the data or set-up, or too little valid evidence. This does not mean the strategy failed.',
    tone: 'neutral',
  },
  'no-edge': {
    label: 'Weak',
    sentence:
      'No reliable profit after costs in this run. Do not move this version forward as it stands.',
    tone: 'bad',
  },
  none: {
    label: 'Not tested',
    sentence: 'No backtest has been recorded for this strategy yet.',
    tone: 'neutral',
  },
}

export const RUN_VERDICT_TO_VERDICT: Record<RunVerdictValue, Verdict> = {
  EDGE: 'edge',
  MARGINAL: 'marginal',
  NO_EDGE: 'no-edge',
  INCONCLUSIVE: 'inconclusive',
}

const TONE_CLASS: Record<PlainVerdictText['tone'], string> = {
  good: 'border-green-600/60 bg-green-600/10',
  warn: 'border-amber-500/60 bg-amber-500/10',
  bad: 'border-red-500/60 bg-red-500/10',
  neutral: 'border-border bg-muted/40',
}

/** The tint of a verdict's banner. Colour supports the label, it never replaces it. */
export function verdictToneClass(verdict: Verdict): string {
  return TONE_CLASS[(PLAIN_VERDICT[verdict] ?? PLAIN_VERDICT.none).tone]
}

/** The verdict as a text chip. The label carries the meaning, colour only supports it. */
export function PlainVerdictChip({ verdict, className }: { verdict: Verdict; className?: string }) {
  const v = PLAIN_VERDICT[verdict] ?? PLAIN_VERDICT.none
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium whitespace-nowrap',
        TONE_CLASS[v.tone],
        className
      )}
      data-testid="plain-verdict-chip"
    >
      {v.label}
    </span>
  )
}

/** The chip with its one-sentence meaning. */
export function PlainVerdictLine({ verdict }: { verdict: Verdict }) {
  const v = PLAIN_VERDICT[verdict] ?? PLAIN_VERDICT.none
  return (
    <p className="flex flex-wrap items-center gap-2 text-sm" data-testid="plain-verdict-line">
      <PlainVerdictChip verdict={verdict} />
      <span>{v.sentence}</span>
    </p>
  )
}
