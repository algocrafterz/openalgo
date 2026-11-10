import type { RunDetail, RunRecord } from '@/api/backtestResults'
import { fmtNum, isExtrapolated, THIN_SAMPLE_TRADES } from '@/lib/backtestResults'
import { COVERAGE_LABEL, NOT_RECORDED_FOR_RUN, runBlocks } from './engineBlocks'
import { MetricTip } from './MetricTip'

const NOT_RECORDED = 'not recorded'

interface Fact {
  label: string
  value: string
  /** Where the statement comes from: this run's record, or the engine's own documentation. */
  source: 'run record' | 'engine documentation'
}

interface Groups {
  calculated: Fact[]
  simulated: Fact[]
  assumed: Fact[]
  /** Coverage, look-ahead protection and versions, each from the run record. */
  recorded: Fact[]
  notModelled: string[]
  confidence: string[]
}

const rec = (label: string, value: string | null | undefined): Fact => ({
  label,
  value: value === null || value === undefined || value === '' ? NOT_RECORDED : value,
  source: 'run record',
})

/** A fact the engine writes into newer run records; older runs read "not recorded for this run". */
const recNew = (label: string, value: string | null | undefined): Fact => ({
  ...rec(label, value),
  value: value ? value : NOT_RECORDED_FOR_RUN,
})

/** Fixed list: what this backtester cannot see or does not catch, in trader language. */
export const KNOWN_LIMITATIONS: readonly string[] = [
  'The look-ahead guard protects the normal strategy hooks, but not every unsupported way of reading data. Indicators computed before the run are checked by a separate re-run test on a sample of symbols.',
  'A few side analyses (the live-cap view, the latency test, the perturbation test, the cross-section baseline and the MFE/MAE report) run strategy code without the guard.',
  "Leaks inside a strategy's own indicator preparation are only caught by that sample re-run.",
  'Swing runs do not detect a missing bar before a fill.',
  'A trade that exits in several parts is priced as one sell order, so its brokerage is slightly understated.',
  'A bar that touches both stop and target is resolved against the trade and flagged as uncertain. The flag also appears on some bars where the order is actually known.',
]

const doc = (label: string, value: string): Fact => ({
  label,
  value,
  source: 'engine documentation',
})

const bps = (v: number | null | undefined, suffix = 'bps') =>
  typeof v === 'number' && Number.isFinite(v) ? `${fmtNum(v, 1)} ${suffix}` : null

function sizing(detail: RunDetail | null | undefined): string | null {
  const s = detail?.scope
  const book = detail?.evaluations?.portfolio
  const parts: string[] = []
  if (typeof s?.capital_per_trade === 'number') parts.push(`Rs ${s.capital_per_trade} per trade`)
  if (typeof s?.risk_per_trade_pct === 'number')
    parts.push(`${s.risk_per_trade_pct}% risked per trade`)
  if (s?.portfolio_mode) parts.push(`portfolio mode: ${s.portfolio_mode}`)
  if (book && !book.unavailable && typeof book.slots === 'number') {
    const notional = typeof book.notional_rs === 'number' ? ` of Rs ${book.notional_rs}` : ''
    parts.push(`${book.slots} positions${notional} in the daily portfolio`)
  }
  return parts.length > 0 ? parts.join('; ') : null
}

function guardText(g: ReturnType<typeof runBlocks>['guard']): string | null {
  if (!g) return null
  const guarded =
    g.mode === 'full'
      ? `Checked: the guard watched every bar of ${g.symbols_checked} symbols (${g.sessions_checked} symbol-sessions), ${g.violations} violations.`
      : g.mode === 'partial'
        ? `Partly checked: ${g.symbols_checked} symbols guarded, ${g.symbols_unguarded?.length ?? 'some'} not.`
        : 'Not run: the guard did not watch this run.'
  const t = g.truncation
  const rerun =
    g.lookahead_check === 'run'
      ? t && t.ok === false
        ? `Re-run test on a sample of symbols: found ${t.mismatches ?? 'some'} differences.`
        : 'Re-run test on a sample of symbols: passed.'
      : 'Re-run test on a sample of symbols: not run.'
  return `${guarded} ${rerun}`
}

function missingText(b: ReturnType<typeof runBlocks>): {
  whole: string | null
  bars: string | null
} {
  const u = b.coverage?.universe
  if (!u) return { whole: null, bars: null }
  const part = u.partial_sessions_total
  return {
    whole: `${u.missing_full_sessions_total} whole sessions with no bars at all${typeof u.expected_sessions_after_exclusions_total === 'number' ? ` (of ${u.expected_sessions_after_exclusions_total} expected sessions after exclusions)` : ''}, across ${u.symbols} symbols`,
    bars:
      typeof part === 'number' && typeof u.missing_bars_total === 'number'
        ? `${u.missing_bars_total} bars missing inside ${part} sessions that do exist`
        : 'not checked for this run',
  }
}

function recordedFacts(run: RunRecord, detail?: RunDetail | null): Fact[] {
  const b = runBlocks(run, detail)
  const c = b.coverage
  const period = c?.effective.period
  const missing = missingText(b)
  const excl = c?.effective.sessions_excluded_by_basis.count
  return [
    recNew(
      'Effective test period',
      period?.start && period.end
        ? `${period.start} to ${period.end}, ${c?.effective.sessions_kept} sessions`
        : null
    ),
    recNew(
      'Session basis',
      c
        ? `${c.session_basis.used === 'common' ? 'common sessions (only days every symbol has)' : 'own sessions (each symbol uses its own days)'}${excl ? `, ${excl} sessions dropped by this basis` : ''}`
        : null
    ),
    recNew('Coverage', c ? COVERAGE_LABEL[c.coverage_class] : null),
    recNew('Missing full sessions', missing.whole),
    recNew('Missing bars within sessions (counted separately)', missing.bars),
    recNew('Look-ahead protection', guardText(b.guard)),
    recNew('Engine version', b.engineVersion === null ? null : String(b.engineVersion)),
    recNew(
      'Cost model version',
      b.costModelVersion === null
        ? null
        : Number(b.costModelVersion) >= 2
          ? `version ${b.costModelVersion}: costs priced on the real buy and sell leg, brokerage cap applied per leg`
          : `version ${b.costModelVersion}`
    ),
  ]
}

/** Builds the statements strictly from the run record; anything else reads "not recorded". */
export function buildAssumptionGroups(run: RunRecord, detail?: RunDetail | null): Groups {
  const m = run.metrics
  const scope = detail?.scope
  const dq = detail?.data_quality
  const timeframe = scope?.timeframe ?? run.data?.bar_size
  const warnings = (dq?.items ?? []).filter((i) => i.severity === 'warning')

  const calculated: Fact[] = [
    rec('Trades counted', typeof m.trades === 'number' ? String(m.trades) : null),
    rec(
      'Gross and net result per trade',
      bps(m.gross_bps) && bps(m.net_bps)
        ? `${bps(m.gross_bps)} before costs, ${bps(m.net_bps)} after costs`
        : null
    ),
    rec('Win rate, profit factor, drawdown', 'worked out from the recorded trades'),
  ]

  const simulated: Fact[] = [
    doc('Entry price', "A signal is read on a closed bar and filled at the next bar's open."),
    doc('Stop and target in the same bar', 'When one bar reaches both, the stop is taken.'),
    doc('Gaps', 'When a bar opens beyond the stop, the fill is the open price, not the stop.'),
  ]
  const modelled = detail?.cost_components
  if (modelled) {
    simulated.push(
      rec(
        'Statutory charges',
        'brokerage, STT, exchange charges, GST, SEBI fee and stamp duty applied per trade'
      )
    )
  }

  const slip = scope?.slippage_bps_per_side
  const assumed: Fact[] = [
    rec('Timeframe', timeframe),
    rec('Data source', run.data?.source),
    rec('Stock list', run.data?.universe),
    rec('Data as of', run.data?.as_of),
    rec('Slippage per side', typeof slip === 'number' ? `${fmtNum(slip, 1)} bps` : null),
    rec(
      'Cost model',
      run.costs?.model
        ? `${run.costs.model}, ${fmtNum(run.costs.round_trip_bps, 1)} bps round trip${run.costs.broker?.label ? `, ${run.costs.broker.label}` : ''}`
        : null
    ),
    rec('Rate profile', scope?.profile?.name),
    rec('Position sizing and capital', sizing(detail)),
  ]

  const confidence: string[] = []
  if (!dq) confidence.push('Data checks: not recorded for this run.')
  else if (warnings.length > 0) {
    confidence.push(
      `Data checks raised ${warnings.length} warning${warnings.length === 1 ? '' : 's'}: ${warnings
        .slice(0, 3)
        .map((w) => w.detail)
        .join(' ')}`
    )
  } else if (dq.status === 'PASS') confidence.push('Data checks passed with no warnings.')
  if (dq?.affects_validity) confidence.push('A data warning affects the validity of this run.')
  if (isExtrapolated(m, run.years)) {
    confidence.push('Less than a year of data: ratios such as Sharpe are extrapolated.')
  }
  if (m.trades > 0 && m.trades < THIN_SAMPLE_TRADES) {
    confidence.push(`Only ${m.trades} trades: a thin sample.`)
  }
  if (run.reproducible === false) confidence.push('This run could not be reproduced exactly.')
  if (run.code_dirty) confidence.push('The code had uncommitted changes when this run was made.')
  if (dq?.policy?.corporate_actions) {
    confidence.push(`Corporate actions, as recorded: ${dq.policy.corporate_actions}`)
  }

  return {
    calculated,
    simulated,
    assumed,
    recorded: recordedFacts(run, detail),
    notModelled: [
      'Partial fills',
      'Queue position',
      'Limit-order fills',
      'Exchange price bands',
      'Corporate actions',
      'Moving averages are not reset across a missing session',
    ],
    confidence,
  }
}

function FactList({ title, facts }: { title: string; facts: Fact[] }) {
  return (
    <section className="space-y-1" data-testid={`assumption-group-${title}`}>
      <h4 className="text-sm font-medium">{title}</h4>
      <ul className="space-y-1 text-sm">
        {facts.map((f) => (
          <li key={f.label}>
            <span className="text-muted-foreground">{f.label}: </span>
            <span className={f.value === NOT_RECORDED ? 'text-muted-foreground italic' : ''}>
              {f.value}
            </span>
            <span className="ml-1 text-xs text-muted-foreground">({f.source})</span>
          </li>
        ))}
      </ul>
    </section>
  )
}

/**
 * What this result rests on, in four plain groups, using only what the run record or the engine
 * documentation states. Anything else reads "not recorded". `compact` is the short form shown
 * with the conclusion; the full panel sits under execution.
 */
export function AssumptionsConfidence({
  run,
  detail,
  compact = false,
}: {
  run: RunRecord
  detail?: RunDetail | null
  compact?: boolean
}) {
  const g = buildAssumptionGroups(run, detail)
  if (compact) {
    return (
      <section
        className="max-w-5xl space-y-1 rounded-md border p-3 text-sm"
        data-testid="assumptions-compact"
        aria-label="How far to trust this result"
      >
        <h2 className="text-sm font-semibold text-muted-foreground">
          How far to trust this result
        </h2>
        <p>
          Fills at the next bar&apos;s open, the stop wins when a bar reaches both stop and target,
          and partial fills, queue position, limit orders and price bands are not modelled.{' '}
          <a className="underline decoration-dotted" href="#assumptions-confidence">
            All assumptions
          </a>
        </p>
        {g.confidence.length > 0 && (
          <ul className="list-disc pl-5" data-testid="confidence-notes">
            {g.confidence.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
        )}
      </section>
    )
  }
  return (
    <section
      id="assumptions-confidence"
      className="space-y-3 rounded-md border p-3"
      data-testid="assumptions-confidence"
      aria-label="Assumptions and confidence"
    >
      <h3 className="text-base font-semibold">
        Assumptions and confidence
        <MetricTip metric="slippage" />
      </h3>
      <div className="grid gap-4 md:grid-cols-2">
        <FactList title="Calculated from data" facts={g.calculated} />
        <FactList title="Simulated by the engine" facts={g.simulated} />
        <FactList title="Depends on an assumption" facts={g.assumed} />
        <FactList title="Coverage, look-ahead protection and versions" facts={g.recorded} />
        <section className="space-y-1" data-testid="assumption-group-Not modelled">
          <h4 className="text-sm font-medium">Not modelled</h4>
          <ul className="list-disc space-y-1 pl-5 text-sm">
            {g.notModelled.map((n) => (
              <li key={n}>{n}</li>
            ))}
          </ul>
        </section>
      </div>
      <section className="space-y-1" data-testid="known-limitations">
        <h4 className="text-sm font-medium">Known limitations of this backtester</h4>
        <ul className="list-disc space-y-1 pl-5 text-sm">
          {KNOWN_LIMITATIONS.map((l) => (
            <li key={l}>{l}</li>
          ))}
        </ul>
      </section>
      <section className="space-y-1" data-testid="confidence-section">
        <h4 className="text-sm font-medium">Confidence notes</h4>
        {g.confidence.length === 0 ? (
          <p className="text-sm text-muted-foreground">None recorded.</p>
        ) : (
          <ul className="list-disc space-y-1 pl-5 text-sm">
            {g.confidence.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
        )}
      </section>
    </section>
  )
}
