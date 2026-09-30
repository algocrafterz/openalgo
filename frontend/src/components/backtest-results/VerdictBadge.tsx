import type { Verdict } from '@/api/backtestResults'
import { Badge } from '@/components/ui/badge'

const LABELS: Record<Verdict, string> = {
  edge: 'edge',
  marginal: 'marginal',
  inconclusive: 'inconclusive',
  'no-edge': 'no edge',
  none: 'not tested',
}

/** Text-only, uniformly styled: a verdict is evidence, not a green light. */
export function VerdictBadge({ verdict }: { verdict: Verdict }) {
  return <Badge variant="outline">{LABELS[verdict] ?? verdict}</Badge>
}

export function StatusBadge({ status }: { status: string }) {
  return <Badge variant="secondary">{status}</Badge>
}
