import { useQuery } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router'
import {
  backtestResultsApi,
  type IdeaCard,
  type RunDetail,
  type RunRecord,
  type TradeSubset,
} from '@/api/backtestResults'
import { BacktestScope } from '@/components/backtest-results/BacktestScope'
import { CompareView } from '@/components/backtest-results/CompareView'
import { DataCoverage } from '@/components/backtest-results/DataCoverage'
import { DecisionPanel } from '@/components/backtest-results/DecisionPanel'
import { GlossaryDrawer } from '@/components/backtest-results/GlossaryDrawer'
import { PromotionGate } from '@/components/backtest-results/PromotionGate'
import { RelatedStrategies } from '@/components/backtest-results/RelatedStrategies'
import { Reproducibility } from '@/components/backtest-results/Reproducibility'
import { ResearchNotes } from '@/components/backtest-results/ResearchNotes'
import { RunEvidence, type TradesDrillState } from '@/components/backtest-results/RunEvidence'
import { RunHistory } from '@/components/backtest-results/RunHistory'
import { RunIdentity } from '@/components/backtest-results/RunIdentity'
import { RunTearsheet } from '@/components/backtest-results/RunTearsheet'
import { StrategyDefinition } from '@/components/backtest-results/StrategyDefinition'
import { StatusBadge, VerdictBadge } from '@/components/backtest-results/VerdictBadge'
import { VersionTree } from '@/components/backtest-results/VersionTree'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { useRunDetail } from '@/hooks/useRunDetail'

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

function RunDetailLoading() {
  return (
    <div className="space-y-3" data-testid="run-detail-loading">
      <Skeleton className="h-24 w-full max-w-3xl" />
      <Skeleton className="h-32 w-full max-w-3xl" />
    </div>
  )
}

type IdeaCardLike = NonNullable<Awaited<ReturnType<typeof backtestResultsApi.idea>>['idea']>

function RunDetailBlocks({
  idea,
  run,
  detail,
  isLatestRun,
  onShowTrades,
}: {
  idea: IdeaCardLike
  run: RunRecord
  detail: RunDetail
  isLatestRun: boolean
  onShowTrades?: (subset: TradeSubset) => void
}) {
  return (
    <>
      <RunIdentity
        ideaTitle={idea.title}
        strategyVersion={detail.strategy_version}
        runId={run.run_id}
        changeClasses={detail.change_classes}
        classification={idea.classification ?? null}
        scope={detail.captured ? detail.scope : undefined}
        strategyCategory={idea.strategy_category}
        tags={idea.strategy_tags}
      />
      <DecisionPanel
        verdict={detail.verdict}
        promotion={detail.promotion}
        nextAction={detail.next_action}
        researchState={idea.research_state}
        stage={idea.stage}
        legacyVerdict={idea.verdict}
        runVerdict={run.run_verdict ?? null}
        isLatestRun={isLatestRun}
        captured={detail.captured}
        evaluation={run.evaluation ?? null}
        onShowTrades={onShowTrades}
      />
      <PromotionGate
        promotion={detail.promotion}
        evaluation={run.evaluation ?? null}
        onShowTrades={onShowTrades}
      />
    </>
  )
}

/**
 * The route element is reused when moving between related ideas. Keying the body on the idea id
 * gives each idea fresh selection state from its very first render.
 */
export default function BacktestResultsDetail() {
  const { ideaId = '' } = useParams<{ ideaId: string }>()
  return <BacktestResultsDetailBody key={ideaId} ideaId={ideaId} />
}

function BacktestResultsDetailBody({ ideaId }: { ideaId: string }) {
  const [selected, setSelected] = useState<string | null>(null)
  const [drill, setDrill] = useState<TradesDrillState | null>(null)
  const nonce = useRef(0)
  const selectRun = (id: string) => {
    setSelected(id)
    setDrill(null)
  }
  const openTrades = (subset: TradeSubset) => {
    nonce.current += 1
    setDrill({ subset, nonce: nonce.current })
  }

  const { data, isLoading } = useQuery({
    queryKey: ['backtest-results', ideaId],
    queryFn: () => backtestResultsApi.idea(ideaId),
    enabled: ideaId !== '',
  })

  // Same key and fetcher as the list page, so a cached list costs no extra request.
  const listQuery = useQuery({
    queryKey: ['backtest-results'],
    queryFn: backtestResultsApi.list,
  })

  const loadedRuns = data?.runs ?? []
  const runId = (loadedRuns.find((r) => r.run_id === selected) ?? loadedRuns[loadedRuns.length - 1])
    ?.run_id
  const detailQuery = useRunDetail(ideaId, runId)

  // Opening the drill-down brings its section into view.
  useEffect(() => {
    if (drill) document.getElementById('trades')?.scrollIntoView?.({ block: 'start' })
  }, [drill])

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
  const isLatestRun = !!current && current === latest
  const allIdeas = listQuery.data?.ideas ?? []
  const currentCard: IdeaCard = allIdeas.find((i) => i.id === idea.id) ?? {
    ...idea,
    run_count: runs.length,
    latest_run: null,
  }
  const detail = detailQuery.data?.run
  const detailFailed = detailQuery.isError || detailQuery.data?.status === 'error'

  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <Link to="/backtest-results" className="text-sm underline underline-offset-4">
          All backtests
        </Link>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <h1 className="text-2xl font-bold">
            {idea.id}: {idea.title}
          </h1>
          <GlossaryDrawer />
        </div>
        <div className="flex flex-wrap gap-2">
          <StatusBadge status={idea.status} />
          {/* One chip says it once: a rejected idea with a stated basis shows the basis. */}
          {idea.rejection_basis ? (
            <Badge variant="outline" data-testid="rejection-basis">
              rejected: {idea.rejection_basis}
            </Badge>
          ) : idea.block_reason ? (
            <Badge variant="outline" data-testid="block-reason">
              blocked: {idea.block_reason}
            </Badge>
          ) : (
            !detail &&
            isLatestRun && (
              <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                Latest run verdict
                <VerdictBadge verdict={idea.verdict} />
              </span>
            )
          )}
        </div>
        <p className="text-sm max-w-3xl">{idea.summary}</p>
        {idea.result && <p className="text-sm max-w-3xl">Conclusion: {idea.result}</p>}
        {idea.revisit_when && (
          <p className="text-sm text-muted-foreground max-w-3xl">
            Revisit when: {idea.revisit_when}
          </p>
        )}
      </div>

      {current && detailQuery.isLoading && <RunDetailLoading />}
      {current && detailFailed && (
        <p className="text-sm text-muted-foreground" data-testid="run-detail-unavailable">
          Run detail unavailable
        </p>
      )}
      {current && detail && (
        <RunDetailBlocks
          idea={idea}
          run={current}
          detail={detail}
          isLatestRun={isLatestRun}
          onShowTrades={detail.captured && detail.has_trades ? openTrades : undefined}
        />
      )}

      <StrategyDefinition
        spec={data.spec}
        loading={!!current && detailQuery.isLoading}
        frozen={
          detail?.captured
            ? {
                spec: detail.spec,
                version: detail.strategy_version,
                parentVersion: detail.parent_version,
                branchLabel: detail.branch_label,
                fingerprints: detail.fingerprints,
              }
            : undefined
        }
      />

      {detail?.captured && (
        <>
          <BacktestScope scope={detail.scope} />
          <Reproducibility
            reproducibility={detail.reproducibility}
            fingerprints={detail.fingerprints}
            manifestSha256={detail.manifest_sha256}
            changeClasses={detail.change_classes}
          />
        </>
      )}

      {current && detail && (
        <RunEvidence
          ideaId={ideaId}
          run={current}
          detail={detail}
          drill={drill}
          onOpenTrades={openTrades}
          onCloseTrades={() => setDrill(null)}
        />
      )}

      {runs.length === 0 || !current ? (
        <div className="text-sm text-muted-foreground" data-testid="no-runs">
          No backtest run has been recorded for this idea yet.
        </div>
      ) : (
        <>
          <DataCoverage run={current} />
          <VersionTree
            versions={data.versions ?? null}
            versionsError={data.versions_error ?? null}
            pointers={data.version_pointers}
            runs={runs}
            selectedRunId={current.run_id}
            onSelectRun={selectRun}
          />
          <RunHistory
            runs={runs}
            selectedId={current.run_id}
            onSelect={selectRun}
            versions={data.versions ?? null}
            pointers={data.version_pointers}
          />
          <div className="text-sm text-muted-foreground">
            Showing run {current.run_id} ({current.date}). Select another date above to view it.
          </div>
          <RunTearsheet
            run={current}
            ideaId={ideaId}
            assumptions={detail?.captured ? detail.assumptions : undefined}
            onShowTrades={detail?.captured && detail.has_trades ? openTrades : undefined}
          />
          <CompareView ideaId={ideaId} runs={runs} />
        </>
      )}

      <ResearchNotes notes={data.research_notes} />

      <RelatedStrategies current={currentCard} ideas={allIdeas} />
    </div>
  )
}
