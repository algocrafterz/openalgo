import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'

/**
 * Which trading mode the figures on a Strategy page belong to. Takes the mode the
 * server reported for the data being shown, not the client toggle, so a stale
 * frame during a mode switch is labelled with the mode it actually contains.
 */
export function StrategyModeBadge({ mode }: { mode?: 'live' | 'analyze' }) {
  if (!mode) return null
  const isLive = mode === 'live'
  return (
    <Badge
      variant="outline"
      className={cn(
        'font-semibold',
        isLive
          ? 'bg-rose-500/10 text-rose-600 border-rose-500/40'
          : 'bg-sky-500/10 text-sky-600 border-sky-500/40'
      )}
      title={
        isLive
          ? 'Real broker orders. Figures below are live-mode trades only.'
          : 'Paper trading in the sandbox. Figures below are Analyze-mode trades only.'
      }
    >
      {isLive ? 'LIVE (real money)' : 'ANALYZE (paper)'}
    </Badge>
  )
}
