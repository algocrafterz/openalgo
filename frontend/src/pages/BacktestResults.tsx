import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router'
import { backtestResultsApi, type ResultsSummary } from '@/api/backtestResults'
import { LeaderboardTable } from '@/components/backtest-results/LeaderboardTable'
import { StrategyFilters } from '@/components/backtest-results/StrategyFilters'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import {
  filterIdeas,
  filtersFromParams,
  filtersToParams,
  groupByStrategyCategory,
  type IdeaFilters,
  type SortDir,
  type SortKey,
  sortIdeas,
  VERDICT_ORDER,
} from '@/lib/backtestResults'

const SEARCH_WRITE_MS = 300

const VERDICT_LABELS: Record<string, string> = {
  edge: 'Edge',
  marginal: 'Marginal',
  inconclusive: 'Inconclusive',
  'no-edge': 'No edge',
  none: 'Not tested',
}

function Tile({ label, value }: { label: string; value: number }) {
  return (
    <Card>
      <CardContent className="py-3">
        <div className="text-xs text-muted-foreground">{label}</div>
        <div className="text-2xl font-semibold font-mono">{value}</div>
      </CardContent>
    </Card>
  )
}

function SummaryTiles({ summary }: { summary: ResultsSummary }) {
  return (
    <div
      className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-8 gap-3"
      data-testid="summary-tiles"
    >
      <Tile label="Ideas" value={summary.total_ideas} />
      <Tile label="Ideas with runs" value={summary.ideas_with_runs} />
      <Tile label="Total runs" value={summary.total_runs} />
      {VERDICT_ORDER.map((v) => (
        <Tile key={v} label={VERDICT_LABELS[v]} value={summary.by_verdict[v] ?? 0} />
      ))}
    </div>
  )
}

function Message({ children }: { children: React.ReactNode }) {
  return <div className="py-10 text-center text-sm text-muted-foreground">{children}</div>
}

export default function BacktestResults() {
  const [searchParams, setSearchParams] = useSearchParams()
  const { data, isLoading } = useQuery({
    queryKey: ['backtest-results'],
    queryFn: backtestResultsApi.list,
  })
  const ideas = useMemo(() => data?.ideas ?? [], [data])

  // Values the loaded ideas do not carry are dropped, so a stale link cannot hide every row.
  const { filters: urlFilters, groupByCategory } = useMemo(
    () => filtersFromParams(searchParams, ideas),
    [searchParams, ideas]
  )

  // The search box filters at once; the URL write waits for a pause in typing.
  const [searchText, setSearchText] = useState(urlFilters.search)
  const lastWritten = useRef(urlFilters.search)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    if (urlFilters.search !== lastWritten.current) {
      lastWritten.current = urlFilters.search
      setSearchText(urlFilters.search)
    }
  }, [urlFilters.search])
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current)
    },
    []
  )
  const filters: IdeaFilters = useMemo(
    () => ({ ...urlFilters, search: searchText }),
    [urlFilters, searchText]
  )

  const writeUrl = (next: IdeaFilters, group: boolean) => {
    if (timer.current) clearTimeout(timer.current)
    timer.current = null
    lastWritten.current = next.search
    setSearchParams(filtersToParams(next, group), { replace: true })
  }
  const updateFilters = (next: IdeaFilters) => {
    setSearchText(next.search)
    if (next.search !== filters.search) {
      if (timer.current) clearTimeout(timer.current)
      timer.current = setTimeout(() => writeUrl(next, groupByCategory), SEARCH_WRITE_MS)
    } else {
      writeUrl(next, groupByCategory)
    }
  }
  const [sortKey, setSortKey] = useState<SortKey>('evidence')
  const [sortDir, setSortDir] = useState<SortDir>('asc')

  const visible = useMemo(
    () => sortIdeas(filterIdeas(ideas, filters), sortKey, sortDir),
    [ideas, filters, sortKey, sortDir]
  )

  const groups = useMemo(() => groupByStrategyCategory(visible), [visible])

  const handleSort = (key: SortKey) => {
    if (key === sortKey) {
      setSortDir(sortDir === 'asc' ? 'desc' : 'asc')
    } else {
      setSortKey(key)
      setSortDir(key === 'net' || key === 'gross' || key === 'oos' ? 'desc' : 'asc')
    }
  }

  const resetOrder = () => {
    setSortKey('evidence')
    setSortDir('asc')
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Backtest Results</h1>
        <p className="text-sm text-muted-foreground max-w-3xl">
          Historical evidence for every strategy idea, net of costs. These are measurements of the
          past, not predictions. Every re-run is kept as its own dated record so changes can be
          compared.
        </p>
      </div>

      {isLoading && (
        <div className="space-y-3" data-testid="loading">
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-64 w-full" />
        </div>
      )}

      {!isLoading && data?.status === 'error' && (
        <div role="alert" className="text-sm text-red-600">
          {data.message ?? 'Failed to load backtest results'}
        </div>
      )}

      {!isLoading && data?.status === 'success' && data.available === false && (
        <Message>Backtest research data is not available on this server.</Message>
      )}

      {!isLoading && data?.status === 'success' && data.available !== false && (
        <>
          {data.summary && <SummaryTiles summary={data.summary} />}
          {ideas.length === 0 ? (
            <Message>No ideas are registered yet.</Message>
          ) : (
            <>
              <div className="flex flex-wrap items-end justify-between gap-3">
                <div className="space-y-2">
                  <StrategyFilters
                    ideas={ideas}
                    filters={filters}
                    shown={visible.length}
                    onChange={updateFilters}
                  />
                  <label className="text-sm flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={groupByCategory}
                      onChange={(e) => writeUrl(filters, e.target.checked)}
                    />
                    Group by category
                  </label>
                </div>
                <button
                  type="button"
                  className="text-sm underline underline-offset-4 disabled:no-underline disabled:text-muted-foreground"
                  disabled={sortKey === 'evidence' && sortDir === 'asc'}
                  onClick={resetOrder}
                >
                  Reset to evidence order
                </button>
              </div>
              {visible.length === 0 ? (
                <Message>No ideas match these filters.</Message>
              ) : (
                <div className="space-y-4">
                  {(groupByCategory ? groups : [{ category: '', ideas: visible }]).map((g) => (
                    <Card key={g.category || 'all'}>
                      <CardContent className="overflow-x-auto">
                        {groupByCategory && (
                          <h2 className="text-sm font-semibold pb-2" data-testid="category-heading">
                            {`${g.category} (${g.ideas.length})`}
                          </h2>
                        )}
                        <LeaderboardTable
                          ideas={g.ideas}
                          sortKey={sortKey}
                          sortDir={sortDir}
                          onSort={handleSort}
                        />
                      </CardContent>
                    </Card>
                  ))}
                </div>
              )}
              <p className="text-xs text-muted-foreground">
                Default order puts ideas with recorded runs first, then by verdict, then by most
                recent run. It does not rank by return.
              </p>
            </>
          )}
        </>
      )}
    </div>
  )
}
