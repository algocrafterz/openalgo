import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { backtestResultsApi, type IdeaCard, type ResultsSummary } from '@/api/backtestResults'
import { LeaderboardTable } from '@/components/backtest-results/LeaderboardTable'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import {
  ALL,
  distinct,
  EMPTY_FILTERS,
  filterIdeas,
  type IdeaFilters,
  type SortDir,
  type SortKey,
  sortIdeas,
  VERDICT_ORDER,
} from '@/lib/backtestResults'

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

function FilterSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: string
  options: string[]
  onChange: (v: string) => void
}) {
  return (
    <label className="text-sm flex flex-col gap-1">
      {label}
      <select
        className="border-input bg-background rounded-md border px-2 py-1.5 text-sm"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value={ALL}>All</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </label>
  )
}

function Filters({
  ideas,
  filters,
  onChange,
}: {
  ideas: IdeaCard[]
  filters: IdeaFilters
  onChange: (next: IdeaFilters) => void
}) {
  const set = (patch: Partial<IdeaFilters>) => onChange({ ...filters, ...patch })
  return (
    <div className="flex flex-wrap items-end gap-3">
      <label className="text-sm flex flex-col gap-1">
        Search
        <Input
          value={filters.search}
          placeholder="Id, title or tag"
          onChange={(e) => set({ search: e.target.value })}
          className="w-56"
        />
      </label>
      <FilterSelect
        label="Status"
        value={filters.status}
        options={distinct(ideas, 'status')}
        onChange={(status) => set({ status })}
      />
      <FilterSelect
        label="Verdict"
        value={filters.verdict}
        options={distinct(ideas, 'verdict')}
        onChange={(verdict) => set({ verdict })}
      />
      <FilterSelect
        label="Category"
        value={filters.category}
        options={distinct(ideas, 'category')}
        onChange={(category) => set({ category })}
      />
      <FilterSelect
        label="Horizon"
        value={filters.horizon}
        options={distinct(ideas, 'horizon')}
        onChange={(horizon) => set({ horizon })}
      />
    </div>
  )
}

function Message({ children }: { children: React.ReactNode }) {
  return <div className="py-10 text-center text-sm text-muted-foreground">{children}</div>
}

export default function BacktestResults() {
  const [filters, setFilters] = useState<IdeaFilters>(EMPTY_FILTERS)
  const [sortKey, setSortKey] = useState<SortKey>('evidence')
  const [sortDir, setSortDir] = useState<SortDir>('asc')

  const { data, isLoading } = useQuery({
    queryKey: ['backtest-results'],
    queryFn: backtestResultsApi.list,
  })

  const ideas = data?.ideas ?? []
  const visible = useMemo(
    () => sortIdeas(filterIdeas(ideas, filters), sortKey, sortDir),
    [ideas, filters, sortKey, sortDir]
  )

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
                <Filters ideas={ideas} filters={filters} onChange={setFilters} />
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
                <Card>
                  <CardContent className="overflow-x-auto">
                    <LeaderboardTable
                      ideas={visible}
                      sortKey={sortKey}
                      sortDir={sortDir}
                      onSort={handleSort}
                    />
                  </CardContent>
                </Card>
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
