import { useQuery } from '@tanstack/react-query'
import { backtestResultsApi } from '@/api/backtestResults'

/** The signal-to-position chain behind one trade. Disabled until a trade id is chosen. */
export function useTradeLineage(
  ideaId: string,
  runId: string,
  tradeId: string | null,
  enabled = true
) {
  return useQuery({
    queryKey: ['backtest-results', ideaId, 'run', runId, 'lineage', tradeId],
    queryFn: () => backtestResultsApi.lineage(ideaId, runId, tradeId as string),
    enabled: enabled && Boolean(ideaId) && Boolean(runId) && tradeId !== null,
    staleTime: 5 * 60 * 1000,
  })
}
