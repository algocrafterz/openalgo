import type { TradeSubset } from '@/api/backtestResults'
import { tradeSubsetForGate } from '@/lib/backtestEvidence'

/** Opens the trade drill-down on the subset behind a gate. Renders nothing for other gates. */
export function ShowTradesButton({
  gate,
  onShowTrades,
  className = 'ml-2 text-xs underline',
}: {
  gate: string | null | undefined
  onShowTrades?: (subset: TradeSubset) => void
  className?: string
}) {
  const subset = tradeSubsetForGate(gate)
  if (!onShowTrades || !subset) return null
  return (
    <button type="button" className={className} onClick={() => onShowTrades(subset)}>
      Show trades
    </button>
  )
}
