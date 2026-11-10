import { useQuery } from '@tanstack/react-query'
import { type ReactNode, useEffect, useRef, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router'
import {
  backtestResultsApi,
  type RunDetail,
  type RunRecord,
  type TradeSubset,
  type TuningSummary,
} from '@/api/backtestResults'
import { AssumptionsConfidence } from '@/components/backtest-results/AssumptionsConfidence'
import { BacktestScope } from '@/components/backtest-results/BacktestScope'
import { CompareView } from '@/components/backtest-results/CompareView'
import { DataCoverageTrust } from '@/components/backtest-results/CoverageChip'
import { DataCoverage } from '@/components/backtest-results/DataCoverage'
import { DecisionBrief } from '@/components/backtest-results/DecisionBrief'
import { DecisionPanel } from '@/components/backtest-results/DecisionPanel'
import { DetailSection } from '@/components/backtest-results/DetailSection'
import { ExecutionDiagnostics } from '@/components/backtest-results/ExecutionDiagnostics'
import { ExportMenu } from '@/components/backtest-results/ExportMenu'
import { runBlocks } from '@/components/backtest-results/engineBlocks'
import { GlossaryDrawer } from '@/components/backtest-results/GlossaryDrawer'
import { PromotionGate } from '@/components/backtest-results/PromotionGate'
import {
  PlainVerdictLine,
  RUN_VERDICT_TO_VERDICT,
} from '@/components/backtest-results/plainVerdict'
import { ReportSheet } from '@/components/backtest-results/ReportSheet'
import { ReportCard, ReportHeader, ResultCard } from '@/components/backtest-results/ReportSummary'
import { Reproducibility } from '@/components/backtest-results/Reproducibility'
import { ResearchNotes } from '@/components/backtest-results/ResearchNotes'
import { RunAssessment } from '@/components/backtest-results/RunAssessment'
import { RunEvidence, type TradesDrillState } from '@/components/backtest-results/RunEvidence'
import { RunHistory } from '@/components/backtest-results/RunHistory'
import { RunIdentity } from '@/components/backtest-results/RunIdentity'
import { RunSummary } from '@/components/backtest-results/RunSummary'
import { RunTearsheet } from '@/components/backtest-results/RunTearsheet'
import { SessionCoverageTable } from '@/components/backtest-results/SessionCoverageTable'
import { SignificanceRows, StatisticsTable } from '@/components/backtest-results/StatisticsTable'
import { StrategyDefinition } from '@/components/backtest-results/StrategyDefinition'
import { StrategyDiagnostics } from '@/components/backtest-results/StrategyDiagnostics'
import { ValidationChecks } from '@/components/backtest-results/ValidationChecks'
import { StatusBadge } from '@/components/backtest-results/VerdictBadge'
import { VersionTree } from '@/components/backtest-results/VersionTree'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { useRunDetail } from '@/hooks/useRunDetail'
import { ideaVerdict, rupeePnl } from '@/lib/backtestResults'
import { conclusionSource, randomEntrySentence } from '@/lib/decisionBrief'
import { cn } from '@/lib/utils'
import { validationChecks } from '@/lib/validationChecks'

type SheetId = 'assumptions' | 'rules' | 'validation' | 'history' | 'notes' | 'trades'

const SHEET_IDS: SheetId[] = ['assumptions', 'rules', 'validation', 'history', 'notes', 'trades']

const SHEET_LINKS: { id: SheetId; label: string }[] = [
  { id: 'trades', label: 'All trades' },
  { id: 'assumptions', label: 'Costs and assumptions' },
  { id: 'rules', label: 'Strategy rules' },
  { id: 'validation', label: 'Validation details' },
  { id: 'history', label: 'Run history and compare' },
  { id: 'notes', label: 'Research notes' },
]

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

/** The researcher's own record, in its original technical wording, with the run it is about. */
function ResearcherConclusion({
  idea,
  runs,
  shownRunId,
}: {
  idea: IdeaCardLike
  runs: RunRecord[]
  shownRunId: string | null
}) {
  const basis = idea.rejection_basis ?? idea.block_reason
  return (
    <div className="max-w-5xl space-y-1 text-sm" data-testid="researcher-conclusion">
      <div className="text-xs font-medium">Researcher's recorded conclusion</div>
      <p data-testid="recorded-status">
        Recorded status: {idea.status}
        {basis ? ` (${basis})` : ''}
      </p>
      {idea.result && (
        <>
          <p className="text-xs text-muted-foreground" data-testid="conclusion-source">
            {conclusionSource(idea.result, runs, shownRunId)}
          </p>
          <p>{idea.result}</p>
        </>
      )}
      {idea.revisit_when && (
        <p className="text-muted-foreground">Revisit when: {idea.revisit_when}</p>
      )}
    </div>
  )
}

/** Why the run got its verdict and what to do next. The verdict itself is in the header. */
function ConclusionLevel({
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
  const blocks = runBlocks(run, detail)
  return detail.captured ? (
    <>
      <DecisionBrief idea={idea} run={run} detail={detail} showChart={false} showFigures={false} />
      <DataCoverageTrust coverage={blocks.coverage} warnings={blocks.warnings} />
      <AssumptionsConfidence run={run} detail={detail} compact />
    </>
  ) : (
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
  )
}

/** The system's own reasoning for the verdict: every test result, benchmarks, corrections. */
function DecisionReasoning({
  idea,
  run,
  detail,
  isLatestRun,
  tuning,
  onShowTrades,
}: {
  idea: IdeaCardLike
  run: RunRecord
  detail: RunDetail
  isLatestRun: boolean
  tuning?: TuningSummary[]
  onShowTrades?: (subset: TradeSubset) => void
}) {
  return (
    <Group id="decision" title="How this decision was reached">
      <RunSummary idea={idea} run={run} detail={detail} />
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
      <RunAssessment run={run} detail={detail} tuning={tuning} />
    </Group>
  )
}

/** A titled block inside one of the report's three collapsed groups; it never collapses itself. */
function Group({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section id={id} data-testid={`section-${id}`} className="space-y-3">
      <h3 className="text-sm font-semibold">{title}</h3>
      {children}
    </section>
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
  // The open side view is kept in the address (?view=validation), so a link can point at it.
  const [searchParams, setSearchParams] = useSearchParams()
  const viewParam = searchParams.get('view')
  const sheet: SheetId | null = SHEET_IDS.includes(viewParam as SheetId)
    ? (viewParam as SheetId)
    : null
  const setSheet = (next: SheetId | null) => {
    const params = new URLSearchParams(searchParams)
    if (next) params.set('view', next)
    else params.delete('view')
    setSearchParams(params, { replace: true })
  }
  const selectRun = (id: string) => {
    setSelected(id)
    setDrill(null)
  }
  const openTrades = (subset: TradeSubset) => {
    nonce.current += 1
    setSheet('trades')
    setDrill({ subset, nonce: nonce.current })
  }

  const { data, isLoading } = useQuery({
    queryKey: ['backtest-results', ideaId],
    queryFn: () => backtestResultsApi.idea(ideaId),
    enabled: ideaId !== '',
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
  const detail = detailQuery.data?.run
  const detailFailed = detailQuery.isError || detailQuery.data?.status === 'error'
  // With a captured run the decision brief answers this in plain words, and the record moves
  // into "How this decision was reached"; otherwise the header keeps it.

  const failedDetail = current && detailFailed
  const portfolio = detail?.evaluations?.portfolio
  // A run with no calculated verdict never borrows the latest run's: only the latest run may
  // show the idea's recorded one, and then it says so.
  const calculated = current ? (detail?.verdict?.run_verdict ?? current.run_verdict ?? null) : null
  const verdict = !current
    ? ideaVerdict({ ...idea, latest_run: null })
    : calculated
      ? RUN_VERDICT_TO_VERDICT[calculated]
      : isLatestRun
        ? ideaVerdict({ ...idea, latest_run: null })
        : null
  const canShowTrades = detail?.captured && detail.has_trades
  const blocks = current ? runBlocks(current, detail) : null

  const tradesHandler = canShowTrades ? openTrades : undefined
  const closeSheet = (open: boolean) => {
    if (open) return
    setSheet(null)
    setDrill(null)
  }

  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Link to="/backtest-results" className="text-sm underline underline-offset-4">
          All backtests
        </Link>
      </div>

      {current ? (
        <>
          <ReportHeader title={idea.title} summary={idea.summary} run={current} detail={detail} />
          <ResultCard
            idea={idea}
            run={current}
            detail={detail}
            portfolio={portfolio}
            verdict={verdict}
            verdictIsLatestRecorded={!calculated}
          />
          <RunTearsheet part="chart" run={current} ideaId={ideaId} portfolio={portfolio} />
          <ReportCard testId="section-performance">
            <StatisticsTable run={current} portfolio={portfolio} />
            <div className="space-y-2">
              <DetailSection id="monthly" title="Monthly returns" summary="year by month">
                <RunTearsheet part="monthly" run={current} ideaId={ideaId} portfolio={portfolio} />
              </DetailSection>
              <DetailSection
                id="diagnostics"
                title="Results by instrument, weekday and exit"
                summary="best and worst instruments, entry hour, exit reason and side"
              >
                <StrategyDiagnostics run={current} diagnostics={detail?.diagnostics} />
              </DetailSection>
            </div>
          </ReportCard>
        </>
      ) : (
        <div className="space-y-2">
          <h1 className="text-2xl font-bold">{idea.title}</h1>
          <p className="text-sm max-w-3xl">{idea.summary}</p>
          <PlainVerdictLine verdict={verdict ?? 'none'} />
          <div className="flex flex-wrap gap-2">
            <StatusBadge status={idea.status} />
            {idea.rejection_basis ? (
              <Badge variant="outline" data-testid="rejection-basis">
                rejected: {idea.rejection_basis}
              </Badge>
            ) : (
              idea.block_reason && (
                <Badge variant="outline" data-testid="block-reason">
                  blocked: {idea.block_reason}
                </Badge>
              )
            )}
          </div>
        </div>
      )}

      {current && detailQuery.isLoading && <RunDetailLoading />}
      {failedDetail && (
        <p className="text-sm text-muted-foreground" data-testid="run-detail-unavailable">
          Run detail unavailable
        </p>
      )}

      {runs.length === 0 || !current ? (
        <div className="text-sm text-muted-foreground" data-testid="no-runs">
          No backtest run has been recorded for this idea yet.
        </div>
      ) : (
        <nav
          aria-label="Report details"
          className="flex flex-wrap gap-x-5 gap-y-2 text-sm"
          data-testid="report-links"
        >
          {SHEET_LINKS.map((l) => (
            <button
              key={l.id}
              type="button"
              className={cn('underline underline-offset-4', l.id === 'trades' && 'font-semibold')}
              disabled={l.id === 'trades' && !tradesHandler}
              onClick={() => (l.id === 'trades' ? openTrades('all') : setSheet(l.id))}
            >
              {l.id === 'trades' ? `All ${current.metrics.trades ?? 0} trades` : l.label}
            </button>
          ))}
        </nav>
      )}

      {current && (
        <>
          <ReportSheet
            title="Trades"
            description="Every simulated trade. Click a heading to sort."
            open={sheet === 'trades'}
            onOpenChange={closeSheet}
            testId="sheet-trades"
          >
            {detail && (
              <RunEvidence
                ideaId={ideaId}
                run={current}
                detail={detail}
                drill={drill}
                omitCosts
                part="trades"
                openByDefault
                onOpenTrades={openTrades}
                onCloseTrades={() => setDrill(null)}
              />
            )}
          </ReportSheet>
          <ReportSheet
            title="Costs and assumptions"
            description="Costs, slippage and fill assumptions behind the net result, and what is not modelled."
            open={sheet === 'assumptions'}
            onOpenChange={closeSheet}
            testId="sheet-assumptions"
          >
            {rupeePnl(current)?.basis && (
              <p className="text-sm text-muted-foreground" data-testid="pnl-basis">
                How the net P&amp;L was sized: {rupeePnl(current)?.basis}
              </p>
            )}
            <ExecutionDiagnostics run={current} detail={detail} onShowTrades={tradesHandler} />
          </ReportSheet>
          <ReportSheet
            title="Strategy rules"
            description="The rules, parameters, scope and dataset this run used."
            open={sheet === 'rules'}
            onOpenChange={closeSheet}
            testId="sheet-rules"
          >
            <StrategyDefinition
              spec={data.spec}
              loading={detailQuery.isLoading}
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
          </ReportSheet>
          <ReportSheet
            title="Validation details"
            description="Whether to pursue this strategy and why, the checks behind it, and the full audit."
            open={sheet === 'validation'}
            onOpenChange={closeSheet}
            testId="sheet-validation"
          >
            <div className="flex justify-end">
              <GlossaryDrawer />
            </div>
            {detail && (
              <ConclusionLevel
                idea={idea}
                run={current}
                detail={detail}
                isLatestRun={isLatestRun}
                onShowTrades={tradesHandler}
              />
            )}
            {detail?.captured && (
              <ValidationChecks
                groups={validationChecks(
                  current.evaluation,
                  detail.evaluations?.corrections,
                  detail.promotion,
                  randomEntrySentence(current)
                )}
              />
            )}
            <DetailSection
              id="audit"
              title="Audit"
              summary="every gate, test and data check behind the verdict"
            >
              {detail?.captured && (
                <DecisionReasoning
                  idea={idea}
                  run={current}
                  detail={detail}
                  isLatestRun={isLatestRun}
                  tuning={data.tuning ?? []}
                  onShowTrades={tradesHandler}
                />
              )}
              {detail?.captured && (
                <Group id="gates" title="Gate-by-gate evidence">
                  <PromotionGate
                    promotion={detail.promotion}
                    evaluation={current.evaluation ?? null}
                    onShowTrades={tradesHandler}
                    stages={['backtest']}
                    title="Backtest gates"
                  />
                </Group>
              )}
              <Group id="tests" title="Statistical tests and robustness">
                <SignificanceRows run={current} />
                <RunTearsheet
                  part="advanced"
                  run={current}
                  assumptions={detail?.captured ? detail.assumptions : undefined}
                  onShowTrades={tradesHandler}
                  corrections={detail?.evaluations?.corrections}
                />
              </Group>
              {detail && (
                <Group id="evidence" title="Run evidence">
                  <RunEvidence
                    ideaId={ideaId}
                    run={current}
                    detail={detail}
                    drill={drill}
                    omitCosts
                    part="evidence"
                    onOpenTrades={openTrades}
                    onCloseTrades={() => setDrill(null)}
                  />
                </Group>
              )}
              <Group id="coverage" title="Data coverage by symbol">
                <SessionCoverageTable coverage={blocks?.coverage ?? null} />
              </Group>
            </DetailSection>
            {detail?.captured && detail.promotion && (
              <DetailSection
                id="readiness"
                title="Paper and live readiness"
                summary="whether the strategy is ready to run; not part of this backtest's verdict"
              >
                <PromotionGate
                  promotion={detail.promotion}
                  evaluation={current.evaluation ?? null}
                  onShowTrades={tradesHandler}
                  stages={['paper', 'live']}
                  title="Readiness gates"
                  testId="readiness-gate"
                />
              </DetailSection>
            )}
          </ReportSheet>
          <ReportSheet
            title="Run history and compare"
            description="Every run of this strategy, its versions, and a side-by-side comparison."
            open={sheet === 'history'}
            onOpenChange={closeSheet}
            testId="sheet-history"
          >
            <p className="text-sm text-muted-foreground" data-testid="showing-run">
              Showing run {current.run_id} ({current.date})
              {runs.length > 1
                ? `, the ${isLatestRun ? 'latest' : 'selected'} of ${runs.length} runs.`
                : '.'}
            </p>
            <ExportMenu ideaId={ideaId} runId={current.run_id} hasTrades={current.trades_export} />
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
            <CompareView ideaId={ideaId} runs={runs} />
          </ReportSheet>
          <ReportSheet
            title="Research notes"
            description="Tags, the researcher's recorded conclusion and notes for this strategy."
            open={sheet === 'notes'}
            onOpenChange={closeSheet}
            testId="sheet-notes"
          >
            {detail && (
              <RunIdentity
                ideaTitle={idea.title}
                strategyVersion={detail.strategy_version}
                runId={current.run_id}
                changeClasses={detail.change_classes}
                classification={idea.classification ?? null}
                scope={detail.captured ? detail.scope : undefined}
                strategyCategory={idea.strategy_category}
                tags={idea.strategy_tags}
              />
            )}
            <div className="flex flex-wrap gap-2">
              <StatusBadge status={idea.status} />
              {idea.rejection_basis ? (
                <Badge variant="outline" data-testid="rejection-basis">
                  rejected: {idea.rejection_basis}
                </Badge>
              ) : (
                idea.block_reason && (
                  <Badge variant="outline" data-testid="block-reason">
                    blocked: {idea.block_reason}
                  </Badge>
                )
              )}
            </div>
            <ResearcherConclusion idea={idea} runs={runs} shownRunId={current.run_id} />
            <ResearchNotes notes={data.research_notes} />
          </ReportSheet>
        </>
      )}
    </div>
  )
}
