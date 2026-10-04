import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { backtestResultsApi, type TradesQuery } from '@/api/backtestResults'

/** One page of a run's trades. Run evidence is immutable, so a long staleTime is safe. */
export function useRunTrades(ideaId: string, runId: string, query: TradesQuery, enabled = true) {
  return useQuery({
    queryKey: ['backtest-results', ideaId, 'run', runId, 'trades', query],
    queryFn: () => backtestResultsApi.trades(ideaId, runId, query),
    enabled: enabled && Boolean(ideaId) && Boolean(runId),
    staleTime: 5 * 60 * 1000,
    placeholderData: keepPreviousData,
  })
}
