import type {
  DiagnosticSide,
  PortfolioView,
  ResearchState,
  RunDetail,
  RunRecord,
} from '@/api/backtestResults'
import { fmtNum, fmtPercent, fmtSigned } from '@/lib/backtestResults'
import {
  buildRunSummary,
  type RunSummaryModel,
  type SummaryFinding,
  type SummaryIdea,
  VERDICT_MEANING,
} from '@/lib/runSummary'
import { cn } from '@/lib/utils'
import { InfoTip } from './InfoTip'
import { LineChart } from './LineChart'
import { decisionClass, decisionText } from './PromotionGate'

const EQUITY_COLOR = '#3b82f6'
const DRAWDOWN_COLOR = '#eab308'

const VERDICT_LABEL = {
  EDGE: 'Edge',
  MARGINAL: 'Marginal',
  NO_EDGE: 'No edge',
  INCONCLUSIVE: 'Inconclusive',
} as const

const STATE_LABEL: Record<ResearchState, string> = {
  active: 'Active',
  promising: 'Promising',
  needs_more_evidence: 'Needs more evidence',
  weak_evidence: 'Weak evidence',
  evidence_against_edge: 'Evidence against an edge',
  shelved: 'Shelved',
  rejected: 'Rejected',
  blocked: 'Blocked',
}

function Answer({
  label,
  value,
  meaning,
  term,
  valueClass,
  testId,
}: {
  label: string
  value: string
  meaning: string
  term: string
  valueClass?: string
  testId: string
}) {
  return (
    <div className="rounded-md border p-3 space-y-1" data-testid={testId}>
      <div className="text-xs text-muted-foreground">
        {label} <InfoTip term={term} />
      </div>
      <div className={cn('text-lg font-semibold', valueClass)}>{value}</div>
      <div className="text-xs text-muted-foreground">{meaning}</div>
    </div>
  )
}

function Answers({ s }: { s: RunSummaryModel }) {
  const state = s.researchState ? STATE_LABEL[s.researchState] : 'Not set'
  return (
    <div className="space-y-2">
      <div className="grid gap-2 sm:grid-cols-3">
        <Answer
          testId="summary-verdict"
          label="Quantitative verdict"
          term="run_verdict"
          value={s.verdict ? VERDICT_LABEL[s.verdict] : 'Not calculated'}
          meaning={
            s.verdict
              ? `Calculated from the gates: ${VERDICT_MEANING[s.verdict]}.`
              : 'No verdict was calculated for this run.'
          }
        />
        <Answer
          testId="summary-disposition"
          label="Research disposition"
          term="research_state"
          value={s.rejectionBasis ? `${state} (${s.rejectionBasis})` : state}
          meaning="Set by the researcher after weighing all the evidence, including diagnostics that are not gates."
        />
        <Answer
          testId="summary-promotion"
          label="Promotion eligibility"
          term="promotion_gate"
          value={decisionText(s.promotion)}
          valueClass={decisionClass(s.promotion)}
          meaning="Whether the platform allows the next stage (paper trading) after the backtest."
        />
      </div>
      {s.reconcile && (
        <p className="text-sm text-muted-foreground" data-testid="summary-reconcile">
          {s.reconcile}
        </p>
      )}
    </div>
  )
}

function Figure({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-md border p-2">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="font-mono text-lg font-semibold">{value}</div>
      {hint && <div className="text-xs text-muted-foreground">{hint}</div>}
    </div>
  )
}

const pct = (v: number | null | undefined, digits = 1) =>
  v === null || v === undefined ? 'n/a' : `${fmtSigned(v, digits)}%`

/** A size, never signed: volatility. */
const plain = (v: number | null | undefined, digits = 1) =>
  v === null || v === undefined ? 'n/a' : `${fmtNum(v, digits)}%`

function rupees(v: number | null | undefined): string {
  if (v === null || v === undefined) return 'n/a'
  return v >= 100000 ? `Rs ${fmtNum(v / 100000, v % 100000 === 0 ? 0 : 1)} lakh` : `Rs ${v}`
}

function basisText(p: PortfolioView, run: RunRecord): string {
  const cost =
    run.costs.model === 'flat-round-trip-bps'
      ? `the engine's flat ${fmtNum(run.costs.round_trip_bps, 0)} bps round-trip cost`
      : `the modelled cost (${run.costs.model})`
  const source =
    p.source === 'reconstructed'
      ? `rebuilt on ${p.evaluated_ist?.slice(0, 10) ?? 'n/a'} from the sealed trades and the run's published closes`
      : 'recorded by the run'
  return `${p.slots} positions of ${rupees(p.notional_rs)} (a ${rupees(p.capital_rs)} book, profits not reinvested), marked to each session close, after ${cost}; ${source}.`
}

function PortfolioFigures({ p, run }: { p: PortfolioView | null; run: RunRecord }) {
  if (!p || p.unavailable || !p.stats) {
    return (
      <p className="text-sm" data-testid="summary-portfolio-unavailable">
        Portfolio return and drawdown: not available.{' '}
        {p?.unavailable ??
          'This run recorded no daily portfolio series and none has been rebuilt; per-trade figures below are not a portfolio result.'}
      </p>
    )
  }
  const st = p.stats
  const alt = p.alternate_cost?.stats
  return (
    <div className="space-y-1" data-testid="summary-portfolio">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
        <Figure
          label="Annual return"
          value={pct(st.annual_return_pct)}
          hint={`compound, ${fmtNum(st.years, 1)} years`}
        />
        <Figure label="Total return" value={pct(st.total_return_pct)} hint="on the book" />
        <Figure
          label="Max drawdown"
          value={pct(st.max_drawdown_pct)}
          hint={`${st.max_drawdown_peak ?? ''} to ${st.max_drawdown_trough ?? ''}`}
        />
        <Figure label="Annual volatility" value={plain(st.annual_volatility_pct)} />
        <Figure
          label="Sharpe"
          value={st.sharpe === null || st.sharpe === undefined ? 'n/a' : fmtNum(st.sharpe)}
          hint={`daily returns, all ${st.sessions ?? 'n/a'} sessions`}
        />
      </div>
      <p className="text-xs text-muted-foreground" data-testid="summary-portfolio-basis">
        {basisText(p, run)}
        {alt &&
          ` At the modelled statutory cost plus slippage: ${pct(alt.annual_return_pct)} a year, drawdown ${pct(alt.max_drawdown_pct)}.`}
      </p>
    </div>
  )
}

function Benchmarks({ s }: { s: RunSummaryModel }) {
  const r = s.randomEntry
  const e = s.equalWeight
  if (!r && !e) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="summary-benchmarks">
        No benchmark comparison was recorded for this run.
      </p>
    )
  }
  return (
    <div data-testid="summary-benchmarks" className="space-y-1">
      <div className="text-xs font-medium">Against benchmarks</div>
      <ul className="list-disc pl-5 text-sm space-y-1">
        {r && (
          <li data-testid="summary-random-entry">
            Matched random entry (same stocks, same holding periods): strategy{' '}
            {fmtSigned(r.strategy_bps, 0)} bps against random {fmtSigned(r.random_bps, 0)} bps net
            per trade, difference {fmtSigned(r.excess_bps, 0)} bps
            {r.t_stat === null ? '' : ` (t ${fmtNum(r.t_stat)})`}.
          </li>
        )}
        {e?.strategy && e.benchmark && (
          <li data-testid="summary-equal-weight">
            {equalWeightText(e.title, e.strategy, e.benchmark)}
            <span className="text-xs text-muted-foreground">
              {' '}
              Supporting diagnostic: not a gate and not decisive. Both sides use one equal-weight
              model, so these figures differ from the fixed-notional book above.{' '}
              {e.reproducible
                ? `Reproducible: the script is kept with the run's evaluations (${e.script?.path ?? 'script'}).`
                : 'Not reproducible: a historical diagnostic whose calculation was not kept.'}
            </span>
          </li>
        )}
      </ul>
    </div>
  )
}

function equalWeightText(title: string | undefined, s: DiagnosticSide, b: DiagnosticSide): string {
  return [
    `${title ?? 'Equal-weight universe'}: ${pct(s.annual_return_pct)} against ${pct(b.annual_return_pct)} a year`,
    `volatility ${plain(s.annual_volatility_pct)} against ${plain(b.annual_volatility_pct)}`,
    `drawdown ${pct(s.max_drawdown_pct, 0)} against ${pct(b.max_drawdown_pct, 0)}`,
    `return per unit of volatility ${fmtNum(s.return_per_volatility)} against ${fmtNum(b.return_per_volatility)}.`,
  ].join('; ')
}

function TradeFigures({ run }: { run: RunRecord }) {
  const m = run.metrics
  const flat = run.costs.model === 'flat-round-trip-bps'
  return (
    <p className="text-sm" data-testid="summary-trades">
      {m.trades} trades, {fmtPercent(m.win_rate)} profitable. Mean net return per trade:{' '}
      <span className="font-mono">{pct(m.net_bps / 100, 2)}</span> of the position's notional (
      {fmtSigned(m.net_bps, 0)} bps), after {fmtNum(run.costs.round_trip_bps, 0)} bps{' '}
      {flat ? 'flat engine cost' : 'modelled cost'}
      {flat && run.cost_model?.net_bps !== undefined
        ? `; ${fmtSigned(run.cost_model.net_bps, 0)} bps at the modelled ${fmtNum(run.cost_model.mean_total_bps)} bps`
        : ''}
      . A per-trade average, not a portfolio return.
    </p>
  )
}

function FindingItems({ items, testId }: { items: SummaryFinding[]; testId: string }) {
  return (
    <ul className="list-disc pl-5 text-sm space-y-0.5" data-testid={testId}>
      {items.map((f) => (
        <li key={f.gate}>
          <a className="underline decoration-dotted" href={`#gate-${f.gate}`}>
            {f.gate}
          </a>{' '}
          {f.title}
          {f.hard ? ' (hard)' : ''}: {f.text}
          {f.correctedFrom && (
            <span className="text-xs text-muted-foreground">
              {' '}
              (corrected evaluation; the sealed record says {f.correctedFrom})
            </span>
          )}
        </li>
      ))}
    </ul>
  )
}

function correctionsHeading(s: RunSummaryModel): string {
  const n = s.corrected.length
  const changed = s.corrected.filter((c) => c.changed_status).map((c) => c.gate)
  const status = changed.length
    ? `the status changed for ${changed.join(', ')} only`
    : 'no status changed'
  return `${n} gate${n === 1 ? '' : 's'} re-scored after evaluator fixes (${status}); this summary uses the corrected values and the sealed record is unchanged`
}

function Corrections({ s }: { s: RunSummaryModel }) {
  if (s.corrected.length === 0) return null
  return (
    <details data-testid="summary-corrections" className="text-sm">
      <summary className="cursor-pointer">{correctionsHeading(s)}</summary>
      <ul className="list-disc pl-5 space-y-1 mt-1">
        {s.corrected.map((c) => (
          <li key={c.gate}>
            <span className="font-mono">{c.gate}</span> recorded {c.recorded.status} (
            {c.recorded.evidence}); corrected {c.corrected.status} ({c.corrected.evidence})
            {c.evaluated_ist ? `, ${c.evaluated_ist.slice(0, 10)}` : ''}.
            {c.reason && <div className="text-xs text-muted-foreground">Why: {c.reason}</div>}
          </li>
        ))}
      </ul>
    </details>
  )
}

/** Equity and drawdown of the daily book, in percent of the book. Central to the reading. */
export function PortfolioChart({ portfolio }: { portfolio: PortfolioView | null | undefined }) {
  if (!portfolio || portfolio.unavailable || !portfolio.equity_pct?.length) return null
  return (
    <section
      className="grid gap-4 rounded-md border p-3 lg:grid-cols-2"
      data-testid="portfolio-chart"
      aria-label="Portfolio equity and drawdown"
    >
      <LineChart
        title="Book return (% of the book)"
        unit="%"
        series={[{ label: 'Return %', color: EQUITY_COLOR, points: portfolio.equity_pct }]}
      />
      <LineChart
        title="Drawdown (% below the prior peak)"
        signed
        unit="%"
        series={[
          { label: 'Drawdown %', color: DRAWDOWN_COLOR, points: portfolio.drawdown_pct ?? [] },
        ]}
      />
    </section>
  )
}

/**
 * The decision on one screen: the three separate answers, portfolio results, benchmarks, what
 * held up, what failed, what limits the reading and what to do next. Detail stays below.
 */
export function RunSummary({
  idea,
  run,
  detail,
}: {
  idea: SummaryIdea
  run: RunRecord
  detail: RunDetail
}) {
  const s = buildRunSummary(idea, run, detail)
  return (
    <section className="max-w-5xl space-y-4" data-testid="run-summary" aria-label="Summary">
      <Answers s={s} />
      {s.selectionNote && (
        <p
          className="rounded-md border border-amber-500/60 bg-amber-500/5 p-3 text-sm font-medium"
          data-testid="summary-selection-note"
        >
          {s.selectionNote}
        </p>
      )}
      {s.supportingNote && (
        <p className="text-xs text-muted-foreground" data-testid="summary-supporting-note">
          {s.supportingNote}
        </p>
      )}
      <PortfolioFigures p={s.portfolio} run={run} />
      <PortfolioChart portfolio={s.portfolio} />
      <Benchmarks s={s} />
      <TradeFigures run={run} />
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <div className="text-xs font-medium text-green-700">What held up</div>
          {s.strengths.length > 0 ? (
            <FindingItems items={s.strengths} testId="summary-strengths" />
          ) : (
            <p className="text-sm text-muted-foreground">No decisive gate passed.</p>
          )}
        </div>
        <div>
          <div className="text-xs font-medium text-red-700">Failed criteria</div>
          {s.failed.length > 0 ? (
            <FindingItems items={s.failed} testId="summary-failed" />
          ) : (
            <p className="text-sm text-muted-foreground">No gate failed.</p>
          )}
        </div>
      </div>
      <Corrections s={s} />
      {s.limitations.length > 0 && (
        <div data-testid="summary-limitations">
          <div className="text-xs font-medium">Limits on this evidence</div>
          <ul className="list-disc pl-5 text-sm">
            {s.limitations.map((t) => (
              <li key={t}>{t}</li>
            ))}
          </ul>
        </div>
      )}
      <div className="rounded-md border p-3" data-testid="summary-action">
        <div className="text-xs text-muted-foreground">Recommended next action</div>
        <div className="font-medium">{s.action.text}</div>
        {s.action.why && <div className="text-sm text-muted-foreground">{s.action.why}</div>}
        {s.action.platform && (
          <div className="mt-1 text-xs text-muted-foreground">
            Only if work resumes on a new hypothesis: the platform's next open gate is "
            {s.action.platform.action}"
            {s.action.platform.gate ? ` (${s.action.platform.gate})` : ''}.
          </div>
        )}
      </div>
    </section>
  )
}
