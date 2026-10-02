import { useQuery } from '@tanstack/react-query'
import { backtestResultsApi } from '@/api/backtestResults'

/**
 * Everything a run folder says about one run (verdict, promotion, scope, reproducibility).
 * Disabled until both ids are known. A run folder is immutable once written, so a long
 * staleTime avoids refetching on every mount.
 */
export function useRunDetail(ideaId: string | undefined, runId: string | undefined) {
  return useQuery({
    queryKey: ['backtest-results', ideaId, 'run', runId],
    queryFn: () => backtestResultsApi.run(ideaId as string, runId as string),
    enabled: Boolean(ideaId) && Boolean(runId),
    staleTime: 5 * 60 * 1000,
  })
}
