import { Link, useNavigate } from 'react-router'
import type { IdeaCard, ResearchState } from '@/api/backtestResults'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import {
  fmtNum,
  fmtRupees,
  fmtSigned,
  ideaVerdict,
  isKnown,
  metricText,
  rupeePnl,
  THIN_SAMPLE_TRADES,
} from '@/lib/backtestResults'
import { researchDecision } from '@/lib/runSummary'
import { cn } from '@/lib/utils'
import { CoverageChip } from './CoverageChip'
import { missingOfExpected } from './engineBlocks'
import { MetricTip } from './MetricTip'
import { PLAIN_VERDICT, PlainVerdictChip } from './plainVerdict'

const NOT_AVAILABLE = 'Not available'

function extras(idea: IdeaCard) {
  return idea.latest_run
}

const STOPPED: ResearchState[] = ['rejected', 'shelved', 'evidence_against_edge']

/** The reason line: what the verdict means, plus how many required tests failed. */
export function conclusionSentence(idea: IdeaCard): string {
  const verdict = ideaVerdict(idea)
  const base = PLAIN_VERDICT[verdict].sentence
  const failed = extras(idea)?.hard_failed?.length ?? 0
  if (verdict === 'none' || failed === 0) return base
  return `${base} ${failed} required test${failed === 1 ? '' : 's'} failed.`
}

/** What to do next: stop for a closed idea, otherwise the next action the run recorded. */
export function recommendedAction(idea: IdeaCard): string {
  if (!idea.latest_run) {
    return idea.block_reason ? `On hold: ${idea.block_reason}` : 'Run a first backtest.'
  }
  if (idea.research_state && STOPPED.includes(idea.research_state)) {
    return 'Do not continue this version.'
  }
  const action = extras(idea)?.next_action?.action
  if (!action) return 'No next action was recorded.'
  return action.endsWith('.') ? action : `${action}.`
}

function tradingStyle(idea: IdeaCard): string {
  return idea.classification?.trading_style || idea.horizon || NOT_AVAILABLE
}

function timeframe(idea: IdeaCard): string {
  return idea.classification?.timeframe || idea.latest_run?.data?.bar_size || NOT_AVAILABLE
}

function Figure({ value, hint, tone }: { value: string; hint?: string; tone?: string }) {
  return (
    <div className="text-right">
      <div className={cn('font-mono', value === NOT_AVAILABLE && 'text-muted-foreground', tone)}>
        {value}
      </div>
      {hint && <div className="text-xs text-muted-foreground">{hint}</div>}
    </div>
  )
}

function signTone(value: number | null | undefined): string {
  if (value === null || value === undefined) return ''
  return value > 0 ? 'text-green-600' : value < 0 ? 'text-red-600' : ''
}

function Row({ idea }: { idea: IdeaCard }) {
  const navigate = useNavigate()
  const run = idea.latest_run
  const verdict = ideaVerdict(idea)
  const m = run?.metrics
  const book = extras(idea)?.book ?? null
  const rupees = run ? rupeePnl(run) : null
  const perTrade =
    m && isKnown(m, m.net_bps) ? `${fmtSigned(m.net_bps, 1)} bps per trade` : undefined
  const thin = !!m && m.trades > 0 && m.trades < THIN_SAMPLE_TRADES
  const coverage = run?.session_coverage
  const reason = conclusionSentence(idea)
  const decision = researchDecision(idea)
  const meta = [tradingStyle(idea), timeframe(idea)].filter((v) => v !== NOT_AVAILABLE).join(' / ')
  return (
    <TableRow
      className="cursor-pointer align-top"
      data-testid={`catalogue-row-${idea.id}`}
      onClick={() => navigate(`/backtest-results/${idea.id}`)}
    >
      <TableCell className="whitespace-normal min-w-[14rem] max-w-md">
        <Link
          to={`/backtest-results/${idea.id}`}
          className="font-medium hover:underline"
          onClick={(e) => e.stopPropagation()}
        >
          {idea.title}
        </Link>
        <div className="text-xs text-muted-foreground capitalize">
          {idea.id}
          {meta && ` / ${meta}`}
        </div>
        {run?.period && (
          <div className="text-xs text-muted-foreground" data-testid="catalogue-run-meta">
            {run.period.start} to {run.period.end}. Last run {run.date}
          </div>
        )}
        {/* A complete data set is the normal case and says nothing; only a gap is worth a chip. */}
        {coverage && coverage.coverage_class !== 'COMPLETE' && (
          <div className="pt-1" data-testid="catalogue-coverage">
            <CoverageChip coverageClass={coverage.coverage_class} />
            {missingOfExpected(coverage) && (
              <div
                className="pt-1 text-xs text-muted-foreground whitespace-normal"
                data-testid="catalogue-coverage-denominator"
              >
                Missing {missingOfExpected(coverage)}
              </div>
            )}
          </div>
        )}
      </TableCell>
      {run && m ? (
        <>
          <TableCell>
            <Figure
              value={rupees ? fmtRupees(rupees.net_rs, true) : NOT_AVAILABLE}
              hint={
                book?.total_return_pct != null
                  ? `${fmtSigned(book.total_return_pct, 1)}% whole portfolio`
                  : perTrade
              }
              tone={signTone(rupees?.net_rs)}
            />
          </TableCell>
          <TableCell>
            <Figure
              value={metricText(m, m.profit_factor, (v) => fmtNum(v)).replace('n/a', NOT_AVAILABLE)}
            />
          </TableCell>
          <TableCell>
            <Figure
              value={
                book?.max_drawdown_pct != null
                  ? `${fmtNum(book.max_drawdown_pct, 1)}%`
                  : metricText(m, m.max_dd_r, (v) => `${fmtNum(v, 1)}R`).replace(
                      'n/a',
                      NOT_AVAILABLE
                    )
              }
              hint={
                book?.max_drawdown_pct == null && isKnown(m, m.max_dd_r)
                  ? 'in R, no book'
                  : undefined
              }
            />
          </TableCell>
          <TableCell className="text-right font-mono">
            {m.trades}
            {thin && <div className="text-xs text-amber-600 font-sans">thin sample</div>}
          </TableCell>
        </>
      ) : (
        <TableCell colSpan={4} className="text-sm text-muted-foreground">
          No backtest recorded yet
        </TableCell>
      )}
      <TableCell className="whitespace-normal min-w-[14rem] max-w-lg">
        <PlainVerdictChip verdict={verdict} />
        {decision && (
          <p className="pt-1 text-xs font-medium" data-testid="research-decision-note">
            Research decision: {decision.label}
          </p>
        )}
        {run && (
          <p
            className="line-clamp-2 pt-1 text-xs text-muted-foreground"
            data-testid="conclusion-sentence"
            title={reason}
          >
            {reason}
          </p>
        )}
        {/* The next step is on the report; here it is read aloud and shown on hover only. */}
        <p className="sr-only" data-testid="next-action">
          {recommendedAction(idea)}
        </p>
      </TableCell>
    </TableRow>
  )
}

function CatalogueHead() {
  return (
    <TableHeader>
      <TableRow>
        <TableHead>Strategy</TableHead>
        <TableHead className="text-right">Net P&amp;L</TableHead>
        <TableHead className="text-right">
          Profit factor
          <MetricTip metric="profit_factor" />
        </TableHead>
        <TableHead className="text-right">
          Max drawdown
          <MetricTip metric="max_drawdown" />
        </TableHead>
        <TableHead className="text-right">Trades</TableHead>
        <TableHead>Assessment</TableHead>
      </TableRow>
    </TableHeader>
  )
}

/**
 * One line per strategy that has been tested: net result, risk and how far to trust it. Ideas
 * with no run yet are one collapsed line below, so they do not fill the page.
 */
export function StrategyCatalogue({ ideas }: { ideas: IdeaCard[] }) {
  const tested = ideas.filter((i) => i.latest_run)
  const untested = ideas.filter((i) => !i.latest_run)
  return (
    <div className="space-y-3" data-testid="strategy-catalogue">
      {(tested.length > 0 || untested.length === 0) && (
        <Table>
          <CatalogueHead />
          <TableBody>
            {tested.map((idea) => (
              <Row key={idea.id} idea={idea} />
            ))}
          </TableBody>
        </Table>
      )}
      {untested.length > 0 && (
        <details data-testid="untested-ideas" className="text-sm">
          <summary className="cursor-pointer select-none py-1 text-muted-foreground">
            {untested.length} not tested yet
          </summary>
          <Table>
            <TableBody>
              {untested.map((idea) => (
                <Row key={idea.id} idea={idea} />
              ))}
            </TableBody>
          </Table>
        </details>
      )}
    </div>
  )
}
