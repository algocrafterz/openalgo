import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useParams } from 'react-router'
import { backtestResultsApi } from '@/api/backtestResults'
import { CompareView } from '@/components/backtest-results/CompareView'
import { DataCoverage } from '@/components/backtest-results/DataCoverage'
import { RunHistory } from '@/components/backtest-results/RunHistory'
import { RunTearsheet } from '@/components/backtest-results/RunTearsheet'
import { StatusBadge, VerdictBadge } from '@/components/backtest-results/VerdictBadge'
import { Skeleton } from '@/components/ui/skeleton'

function Failure({ message }: { message: string }) {
  return (
    <div className="space-y-3">
      <div role="alert" className="text-sm text-red-600">
        {message}
      </div>
      <Link to="/backtest-results" className="text-sm underline underline-offset-4">
        Back to all backtests
      </Link>
    </div>
  )
}

export default function BacktestResultsDetail() {
  const { ideaId = '' } = useParams<{ ideaId: string }>()
  const [selected, setSelected] = useState<string | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['backtest-results', ideaId],
    queryFn: () => backtestResultsApi.idea(ideaId),
    enabled: ideaId !== '',
  })

  if (isLoading) {
    return (
      <div className="space-y-3" data-testid="loading">
        <Skeleton className="h-16 w-full" />
        <Skeleton className="h-80 w-full" />
      </div>
    )
  }
  if (!data || data.status === 'error' || !data.idea) {
    return <Failure message={data?.message ?? 'Failed to load backtest history'} />
  }

  const { idea } = data
  const runs = data.runs ?? []
  const latest = runs[runs.length - 1]
  const current = runs.find((r) => r.run_id === selected) ?? latest

  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <Link to="/backtest-results" className="text-sm underline underline-offset-4">
          All backtests
        </Link>
        <h1 className="text-2xl font-bold">
          {idea.id}: {idea.title}
        </h1>
        <div className="flex flex-wrap gap-2">
          <StatusBadge status={idea.status} />
          <VerdictBadge verdict={idea.verdict} />
        </div>
        <p className="text-sm max-w-3xl">{idea.summary}</p>
        {idea.result && <p className="text-sm max-w-3xl">Conclusion: {idea.result}</p>}
        {idea.revisit_when && (
          <p className="text-sm text-muted-foreground max-w-3xl">
            Revisit when: {idea.revisit_when}
          </p>
        )}
      </div>

      {runs.length === 0 || !current ? (
        <div className="text-sm text-muted-foreground" data-testid="no-runs">
          No backtest run has been recorded for this idea yet.
        </div>
      ) : (
        <>
          <DataCoverage run={current} />
          <RunHistory runs={runs} selectedId={current.run_id} onSelect={setSelected} />
          <div className="text-sm text-muted-foreground">
            Showing run {current.run_id} ({current.date}). Select another date above to view it.
          </div>
          <RunTearsheet run={current} />
          <CompareView ideaId={ideaId} runs={runs} />
        </>
      )}
    </div>
  )
}
