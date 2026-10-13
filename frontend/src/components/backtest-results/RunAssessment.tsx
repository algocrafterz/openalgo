import type { RunDetail, RunRecord, TuningSummary } from '@/api/backtestResults'
import { fmtNum, isKnown, metricText } from '@/lib/backtestResults'
import {
  type RunAssessment as Assessment,
  assessRun,
  type GateFinding,
  runUsesSelection,
  tuningStability,
} from '@/lib/runAssessment'
import { cn } from '@/lib/utils'

function Metric({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-md border p-2">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="font-mono font-semibold">{value}</div>
      {hint && <div className="text-xs text-muted-foreground">{hint}</div>}
    </div>
  )
}

function KeyMetrics({ run }: { run: RunRecord }) {
  const m = run.metrics
  const is = run.in_sample
  const oos = run.out_of_sample
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-4" data-testid="assessment-metrics">
      <Metric label="Net R per trade" value={metricText(m, m.expectancy_r)} hint="after costs" />
      <Metric label="Trades" value={m.trades === undefined ? 'n/a' : String(m.trades)} />
      <Metric label="Max drawdown" value={metricText(m, m.max_dd_r, (v) => `${fmtNum(v, 1)}R`)} />
      <Metric
        label="Mean net bps per trade: in / out of sample"
        value={`${is ? metricText(is, is.net_bps) : 'n/a'} / ${oos ? metricText(oos, oos.net_bps) : 'n/a'}`}
        hint={
          run.split?.oos_start
            ? `${is?.trades ?? 'n/a'} / ${oos?.trades ?? 'n/a'} trades; hold-out from ${run.split.oos_start}; trades straddling the cut are purged from in-sample`
            : 'no hold-out split'
        }
      />
    </div>
  )
}

function CostLine({ run }: { run: RunRecord }) {
  const m = run.metrics
  if (!isKnown(m, m.gross_bps) || !isKnown(m, m.net_bps)) return null
  const cost = m.gross_bps - m.net_bps
  if (m.gross_bps <= 0) {
    // No edge to erode: a break-even cost below zero means nothing, so it is not shown.
    return (
      <p className="text-sm" data-testid="assessment-costs">
        No edge before costs: {fmtNum(m.gross_bps, 1)} bps per trade gross. Costs add another{' '}
        {fmtNum(cost, 1)} bps.
      </p>
    )
  }
  const erased = m.net_bps <= 0
  return (
    <p className="text-sm" data-testid="assessment-costs">
      Costs take {fmtNum(cost, 1)} bps of a {fmtNum(m.gross_bps, 1)} bps gross result per trade
      {erased ? ', which removes the whole gross edge.' : '.'}
      {isKnown(m, m.breakeven_cost_bps) &&
        m.breakeven_cost_bps > 0 &&
        ` Break-even cost: ${fmtNum(m.breakeven_cost_bps, 1)} bps per trade.`}
    </p>
  )
}

function ValidityLine({ v }: { v: Assessment['validity'] }) {
  const text =
    v.state === 'valid'
      ? `Valid run: all ${v.checks} integrity checks passed.`
      : v.state === 'invalid'
        ? 'Invalid run: the results are not evidence until these are fixed.'
        : 'Validity not recorded: this run predates integrity checks.'
  return (
    <div data-testid="assessment-validity" data-state={v.state}>
      <div
        className={cn(
          'text-sm font-medium',
          v.state === 'valid' && 'text-green-700',
          v.state === 'invalid' && 'text-red-700',
          v.state === 'not_recorded' && 'text-muted-foreground'
        )}
      >
        {text}
      </div>
      {v.failures.length > 0 && (
        <ul className="list-disc pl-5 text-xs">
          {v.failures.map((f) => (
            <li key={f}>{f}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

function FindingList({ items, testId }: { items: GateFinding[]; testId: string }) {
  return (
    <ul className="list-disc pl-5 text-sm" data-testid={testId}>
      {items.map((f) => (
        <li key={f.gate}>
          <a className="underline decoration-dotted" href={`#gate-${f.gate}`}>
            {f.gate}
          </a>{' '}
          {f.title}: observed {f.observed}, needs {f.threshold || 'n/a'}
          {f.hard ? '' : ' (soft)'}
        </li>
      ))}
    </ul>
  )
}

function Findings({ a }: { a: Assessment }) {
  return (
    <div className="space-y-2" data-testid="assessment-findings">
      {a.shortfalls.length > 0 && (
        <div>
          <div className="text-xs font-medium text-red-700">Fell short of the criteria</div>
          <FindingList items={a.shortfalls} testId="assessment-shortfalls" />
        </div>
      )}
      {a.missing.length > 0 && (
        <div data-testid="assessment-missing-section">
          <div className="text-xs font-medium text-amber-700">
            Not yet established ({a.missing.length}): missing evidence for this run, not a failure
          </div>
          <FindingList items={a.missing.slice(0, MISSING_SHOWN)} testId="assessment-missing" />
          {a.missing.length > MISSING_SHOWN && (
            <details data-testid="assessment-missing-more">
              <summary className="cursor-pointer text-sm">
                Show the other {a.missing.length - MISSING_SHOWN}
              </summary>
              <FindingList
                items={a.missing.slice(MISSING_SHOWN)}
                testId="assessment-missing-rest"
              />
            </details>
          )}
        </div>
      )}
      <details data-testid="assessment-worked">
        <summary className="cursor-pointer text-sm">
          {a.worked.length} checks passed
          {a.shortfalls.length === 0 && a.missing.length === 0 && a.worked.length > 0
            ? ', none failed at this stage'
            : ''}
        </summary>
        <FindingList items={a.worked} testId="assessment-worked-list" />
      </details>
      <p className="text-xs text-muted-foreground" data-testid="assessment-other-counts">
        {a.later} required only at the paper or live stage; {a.notApplicable} do not apply to this
        strategy.
      </p>
    </div>
  )
}

/** The most important applicable gates without evidence, shown before the expander. */
const MISSING_SHOWN = 3

function RecordedUnverified({ items }: { items: string[] }) {
  if (items.length === 0) return null
  return (
    <details data-testid="assessment-recorded-unverified">
      <summary className="cursor-pointer text-sm">
        Recorded at run time as not yet verified ({items.length})
      </summary>
      <p className="text-xs text-muted-foreground">
        What this run's record listed as not yet verified when it was made. It describes that run,
        not the current state of the engine, which the readiness ledger holds.
      </p>
      <ul className="list-disc pl-5 text-sm">
        {items.map((t) => (
          <li key={t}>{t}</li>
        ))}
      </ul>
    </details>
  )
}

function Limitations({ items }: { items: string[] }) {
  if (items.length === 0) return null
  return (
    <div data-testid="assessment-limitations">
      <div className="text-xs font-medium">Limitations that affect how to read this</div>
      <ul className="list-disc pl-5 text-sm">
        {items.map((t) => (
          <li key={t}>{t}</li>
        ))}
      </ul>
    </div>
  )
}

function valuesText(values: Record<string, unknown> | undefined): string {
  return Object.entries(values ?? {})
    .map(([k, v]) => `${k}=${String(v)}`)
    .join(', ')
}

function TuningEvidence({ tuning, run }: { tuning: TuningSummary[]; run: RunRecord }) {
  const unreadable = tuning.filter((t) => t.error)
  const latest = [...tuning].reverse().find((t) => !t.error)
  return (
    <div data-testid="assessment-tuning">
      <div className="text-xs font-medium">Parameter tuning</div>
      {!latest ? (
        <p className="text-sm" data-testid="tuning-none">
          Not performed. These are the strategy's own parameter values; how nearby settings would do
          has not been tested.
        </p>
      ) : (
        <TuningRecord t={latest} run={run} />
      )}
      {unreadable.map((t) => (
        <p key={t.tuning_id} className="text-sm text-red-700">
          Tuning record {t.tuning_id} could not be read.
        </p>
      ))}
    </div>
  )
}

function TuningRecord({ t, run }: { t: TuningSummary; run: RunRecord }) {
  const sel = t.selected
  const stability = tuningStability(t)
  const uses = runUsesSelection(run, t)
  return (
    <ul className="list-disc pl-5 text-sm" data-testid="tuning-record">
      <li>
        {t.combinations} settings tried ({t.usable} with enough trades) on sessions up to{' '}
        {t.in_sample_end}; sessions from {t.held_out_from} were not read.
      </li>
      <li data-testid="tuning-constraints">
        {t.constraints?.fully_constrained
          ? 'Ranked with the live portfolio limits applied (open positions, trades per day, loss limits).'
          : 'Not a fully constrained selection: settings were ranked without the live portfolio limits, so the ranking may change once they apply.'}
      </li>
      {sel ? (
        <>
          <li>
            Selected {valuesText(sel.values)}: {fmtNum(sel.net_r, 3)} R per trade in-sample.
          </li>
          <li data-testid="tuning-stability" data-stability={stability}>
            {stability === 'stable' &&
              `Stable: all ${sel.region_size} usable neighbouring settings (of ${sel.region_possible ?? 'n/a'} nearby) were positive in-sample; the worst made ${fmtNum(sel.region_worst_net_r, 3)} R per trade.`}
            {stability === 'fragile' &&
              `Fragile: a neighbouring setting made ${fmtNum(sel.region_worst_net_r, 3)} R per trade in-sample, so the result depends on a narrow choice of values.`}
            {stability === 'unknown' && 'Too few usable neighbouring settings to judge stability.'}
          </li>
          {t.selected_is_not_the_single_best && t.best_single && (
            <li>
              The single best setting ({valuesText(t.best_single.values)},{' '}
              {fmtNum(t.best_single.net_r, 3)} R) was not chosen because its neighbours were weaker.
            </li>
          )}
        </>
      ) : (
        <li>No setting had enough trades to be selected.</li>
      )}
      {uses !== null && (
        <li data-testid="tuning-run-match">
          {uses
            ? 'This run uses the selected values.'
            : 'This run does not use the selected values.'}
        </li>
      )}
      {t.pbo?.value !== null && t.pbo?.value !== undefined && (
        <li className="text-muted-foreground">
          Overfitting diagnostic (PBO of picking the single best setting): {fmtNum(t.pbo.value, 2)}.
          Diagnostic only, not a gate.
        </li>
      )}
    </ul>
  )
}

function Recommendations({ a }: { a: Assessment }) {
  if (a.recommendations.length === 0) return null
  return (
    <div data-testid="assessment-recommendations">
      <div className="text-xs font-medium">What to look at next</div>
      <ul className="list-disc pl-5 text-sm space-y-1">
        {a.recommendations.map((r) => (
          <li key={r.evidence}>
            {r.text}
            <div className="text-xs text-muted-foreground">Evidence: {r.evidence}</div>
          </li>
        ))}
      </ul>
    </div>
  )
}

/**
 * How good the recorded result is, in a few lines: validity, the metrics that matter, what passed
 * and what fell short, limitations, tuning evidence and evidence-linked follow-ups. It reads the
 * run record and the idea's tuning records only; the verdict and promotion above stay the source
 * of truth.
 */
export function RunAssessment({
  run,
  detail,
  tuning = [],
}: {
  run: RunRecord
  detail: RunDetail
  tuning?: TuningSummary[]
}) {
  const a = assessRun(run, detail, tuning)
  return (
    <section
      className="max-w-3xl space-y-3 rounded-md border p-3"
      data-testid="run-assessment"
      aria-label="Assessment"
    >
      <div className="text-sm font-semibold">How good is this result?</div>
      <ValidityLine v={a.validity} />
      <KeyMetrics run={run} />
      <CostLine run={run} />
      <Findings a={a} />
      <Limitations items={a.limitations} />
      <RecordedUnverified items={a.recordedUnverified} />
      <TuningEvidence tuning={tuning} run={run} />
      <Recommendations a={a} />
      <p className="text-xs text-muted-foreground">
        Historical results only, not a forecast. Eligibility for paper trading is not approval for
        live trading.
      </p>
    </section>
  )
}
