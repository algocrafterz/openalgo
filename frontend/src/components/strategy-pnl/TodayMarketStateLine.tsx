import { useQuery } from '@tanstack/react-query'
import { strategyPnlApi } from '@/api/strategyPnl'
import { Badge } from '@/components/ui/badge'

export const TODAY_MARKET_STATE_POLL_MS = 5 * 60_000

/**
 * Read-only context line: the NIFTY 50 daily market state that applies to the current session
 * (the previous completed trading day). Shows nothing while loading or when it cannot be read,
 * so it never adds an error to the page it sits on.
 */
export function TodayMarketStateLine() {
  const { data } = useQuery({
    queryKey: ['strategy-market-state-today'],
    queryFn: () => strategyPnlApi.marketStateToday(),
    refetchInterval: TODAY_MARKET_STATE_POLL_MS,
    staleTime: TODAY_MARKET_STATE_POLL_MS / 2,
  })
  if (!data || data.status !== 'success') return null
  return (
    <div
      className="mt-2 flex flex-wrap items-center gap-2 text-sm"
      data-testid="today-market-state"
    >
      <span className="text-muted-foreground">Today's market state:</span>
      <Badge variant="outline" data-testid="today-trend">
        {data.trend ?? 'Unclassified'}
      </Badge>
      <Badge variant="outline" data-testid="today-volatility">
        {data.volatility ?? 'Unclassified'} volatility
      </Badge>
      <span className="text-xs text-muted-foreground">
        {data.as_of ? `as of ${data.as_of}, ` : ''}
        {data.benchmark ?? 'NIFTY 50 (daily)'}
      </span>
    </div>
  )
}
