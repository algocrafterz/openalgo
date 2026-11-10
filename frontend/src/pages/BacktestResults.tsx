import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router'
import { backtestResultsApi } from '@/api/backtestResults'
import { PLAIN_VERDICT } from '@/components/backtest-results/plainVerdict'
import { StrategyCatalogue } from '@/components/backtest-results/StrategyCatalogue'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import {
  ALL,
  EMPTY_FILTERS,
  filterIdeas,
  filtersFromParams,
  filtersToParams,
  type IdeaFilters,
  sortIdeas,
  VERDICT_ORDER,
} from '@/lib/backtestResults'

const SEARCH_WRITE_MS = 300

const ASSESSMENTS: { value: string; label: string }[] = VERDICT_ORDER.filter(
  (v) => v !== 'none'
).map((v) => ({ value: v, label: PLAIN_VERDICT[v].label }))

/** Search and one Assessment filter. Everything else about a strategy is on its report. */
function DashboardFilters({
  filters,
  onChange,
}: {
  filters: IdeaFilters
  onChange: (next: IdeaFilters) => void
}) {
  const set = (patch: Partial<IdeaFilters>) => onChange({ ...filters, ...patch })
  return (
    <div className="flex flex-wrap items-center gap-3" data-testid="dashboard-filters">
      <Input
        aria-label="Search strategies"
        placeholder="Search strategies"
        value={filters.search}
        onChange={(e) => set({ search: e.target.value })}
        className="w-60"
      />
      <label className="flex items-center gap-2 text-sm">
        Assessment
        <select
          className="border-input bg-background rounded-md border px-2 py-1.5 text-sm"
          value={filters.verdict}
          onChange={(e) => set({ verdict: e.target.value })}
        >
          <option value={ALL}>All</option>
          {ASSESSMENTS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </label>
      {(filters.search || filters.verdict !== ALL) && (
        <button
          type="button"
          className="text-sm underline underline-offset-4"
          onClick={() => onChange({ ...EMPTY_FILTERS, tags: [] })}
        >
          Clear
        </button>
      )}
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
  const { filters: urlFilters } = useMemo(
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

  const writeUrl = (next: IdeaFilters) => {
    if (timer.current) clearTimeout(timer.current)
    timer.current = null
    lastWritten.current = next.search
    setSearchParams(filtersToParams(next, false), { replace: true })
  }
  const updateFilters = (next: IdeaFilters) => {
    setSearchText(next.search)
    if (next.search !== filters.search) {
      if (timer.current) clearTimeout(timer.current)
      timer.current = setTimeout(() => writeUrl(next), SEARCH_WRITE_MS)
    } else {
      writeUrl(next)
    }
  }

  // Tested strategies first, most recent run first; the plain order is the order they were added.
  const visible = useMemo(
    () => sortIdeas(filterIdeas(ideas, filters), 'evidence', 'asc'),
    [ideas, filters]
  )

  return (
    <div className="mx-auto max-w-7xl space-y-4">
      <h1 className="text-2xl font-bold">Backtests</h1>

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

      {!isLoading &&
        data?.status === 'success' &&
        data.available !== false &&
        (ideas.length === 0 ? (
          <Message>No ideas are registered yet.</Message>
        ) : (
          <>
            <DashboardFilters filters={filters} onChange={updateFilters} />
            {visible.length === 0 ? (
              <Message>No strategies match these filters.</Message>
            ) : (
              <Card>
                <CardContent className="overflow-x-auto">
                  <StrategyCatalogue ideas={visible} />
                </CardContent>
              </Card>
            )}
          </>
        ))}
    </div>
  )
}
