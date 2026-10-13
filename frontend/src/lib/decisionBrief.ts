import type { PortfolioView, ResearchState, RunDetail, RunRecord } from '@/api/backtestResults'
import { fmtNum, fmtPercent, fmtSigned } from './backtestResults'
import { type EffectiveRow, effectiveRows, type SummaryIdea } from './runSummary'

/**
 * The trader's reading of one run: should I pursue it, how did it perform, why, and what next.
 * Built only from recorded fields; nothing here is scored or invented. Gate ids, statistics and
 * evaluator history stay in the detail sections, reachable through each finding's link.
 */

export type BriefTone = 'stop' | 'wait' | 'go' | 'unknown'

export interface BriefFigure {
  label: string
  /** 'Not available' when the run has no such figure, never zero. */
  value: string
  hint: string
}

export interface BriefFinding {
  text: string
  /** In-page anchor of the evidence behind the finding. */
  href: string
}

export interface DecisionBriefModel {
  headline: string
  tone: BriefTone
  reason: string
  figures: BriefFigure[]
  basis: string
  portfolio: PortfolioView | null
  findings: BriefFinding[]
  /** Failed tests not shown as findings; they are listed in the details. */
  moreFailed: number
  supporting: string | null
  caveats: string[]
  action: { text: string; why: string }
}

const MAX_FINDINGS = 3

const STOPPED: ResearchState[] = ['rejected', 'shelved', 'evidence_against_edge']

const HEADLINE: Record<ResearchState, { text: string; tone: BriefTone }> = {
  rejected: { text: 'Do not pursue this version.', tone: 'stop' },
  shelved: { text: 'Do not pursue this version.', tone: 'stop' },
  evidence_against_edge: { text: 'Do not pursue this version.', tone: 'stop' },
  weak_evidence: { text: 'Not yet: the evidence for an edge is weak.', tone: 'wait' },
  needs_more_evidence: { text: 'Not yet: more evidence is needed.', tone: 'wait' },
  active: { text: 'Not yet: research is still in progress.', tone: 'wait' },
  promising: { text: 'Keep researching: the evidence so far is promising.', tone: 'go' },
  blocked: { text: 'On hold: research is blocked.', tone: 'wait' },
}

/** One plain sentence per failed test. Unlisted gates fall back to their title. */
const FAILED_TEXT: Record<string, string> = {
  'G1.3': 'It does not survive higher trading costs (slippage stress test).',
  'G2.2': 'The profit could be luck: it failed the statistical significance test.',
  'G2.3': 'It failed the significance test that allows for the number of variants tried.',
  'G3.3': 'It did not hold up across rolling time windows (walk-forward test).',
  'G3.4': 'It did not hold up on a held-out half of the stocks.',
  'G4.1': 'Small changes to its settings broke the result.',
  'G4.4': 'It did not work consistently across separate time periods.',
  'G4.5': 'The profit depends on too few stocks.',
}

const count = (n: number) => n.toLocaleString('en-IN')
const pctOfBps = (bps: number) => `${fmtSigned(bps / 100, 2)}%`
const pct = (v: number | null | undefined, digits = 1) =>
  v === null || v === undefined ? 'Not available' : `${fmtSigned(v, digits)}%`

function rupees(v: number | null | undefined): string {
  if (v === null || v === undefined) return 'n/a'
  return v >= 100000 ? `Rs ${fmtNum(v / 100000, v % 100000 === 0 ? 0 : 1)} lakh` : `Rs ${v}`
}

function bookStats(p: PortfolioView | null) {
  return p && !p.unavailable && p.stats ? p.stats : null
}

function isProfitable(run: RunRecord, p: PortfolioView | null): boolean {
  const st = bookStats(p)
  if (st) return (st.total_return_pct ?? 0) > 0
  return (run.metrics?.net_bps ?? 0) > 0
}

function costText(run: RunRecord): string {
  if (run.costs.model === 'flat-round-trip-bps') {
    return `the engine's flat ${fmtNum(run.costs.round_trip_bps, 0)} bps round-trip cost`
  }
  const broker = run.costs.broker?.label
  return `modelled ${broker ? `${broker} ` : ''}costs (${fmtNum(run.costs.round_trip_bps, 1)} bps round trip on average)`
}

function headline(idea: SummaryIdea): { text: string; tone: BriefTone } {
  if (idea.research_state) return HEADLINE[idea.research_state]
  return { text: 'No research decision has been recorded yet.', tone: 'unknown' }
}

function reason(
  run: RunRecord,
  p: PortfolioView | null,
  failed: EffectiveRow[],
  selectionUnproven: boolean
): string {
  const subject = bookStats(p) ? 'The portfolio' : 'The strategy'
  if (!isProfitable(run, p)) {
    return (run.metrics.gross_bps ?? 0) <= 0
      ? `${subject} lost money, even before trading costs.`
      : `${subject} lost money after trading costs.`
  }
  if (selectionUnproven) {
    return `${subject} made money, but the tests did not show that the strategy's selection rule caused the profit.`
  }
  const hard = failed.filter((r) => r.hard).length
  if (hard > 0)
    return `${subject} made money, but ${hard} required test${hard === 1 ? '' : 's'} failed.`
  if (failed.length > 0) return `${subject} made money, but some tests failed.`
  return `${subject} made money and no test failed.`
}

function figures(run: RunRecord, p: PortfolioView | null): BriefFigure[] {
  const m = run.metrics
  const st = bookStats(p)
  const none = 'no portfolio series for this run'
  const drawdownHint =
    st?.max_drawdown_peak && st.max_drawdown_trough
      ? `peak ${st.max_drawdown_peak} to low ${st.max_drawdown_trough}`
      : st
        ? 'below a portfolio peak'
        : none
  const out: BriefFigure[] = [
    {
      label: 'Annual return',
      value: pct(st?.annual_return_pct),
      hint: st ? 'whole portfolio, compounded per year' : none,
    },
    { label: 'Worst fall (max drawdown)', value: pct(st?.max_drawdown_pct), hint: drawdownHint },
  ]
  if (!st) {
    out.push({
      label: 'Average per trade',
      value: pctOfBps(m.net_bps),
      hint: 'after costs, per position; not a portfolio return',
    })
  }
  out.push(
    {
      label: 'Trades',
      value: count(m.trades),
      hint: typeof m.win_rate === 'number' ? `${fmtPercent(m.win_rate)} profitable` : '',
    },
    {
      label: 'Test period',
      value: run.period ? `${run.period.start} to ${run.period.end}` : 'Not available',
      hint:
        st?.first_session && st.last_session
          ? `portfolio measured ${st.first_session} to ${st.last_session} (${fmtNum(st.years, 1)} years)`
          : '',
    }
  )
  return out
}

function basis(run: RunRecord, p: PortfolioView | null): string {
  if (!p || !bookStats(p)) {
    return `No whole-portfolio return was recorded for this run. The average per trade is after ${costText(run)} and is not a portfolio return.`
  }
  const alt = p.alternate_cost?.stats
  const modelled = alt
    ? ` With modelled statutory costs and slippage instead: ${pct(alt.annual_return_pct)} a year, worst fall ${pct(alt.max_drawdown_pct)}.`
    : ''
  const rebuilt =
    p.source === 'reconstructed' ? ' Rebuilt after the run from its sealed trades.' : ''
  return `Portfolio of ${p.slots} positions of ${rupees(p.notional_rs)} each (profits not reinvested), after ${costText(run)}.${modelled}${rebuilt}`
}

function lossFinding(run: RunRecord): BriefFinding {
  const m = run.metrics
  const after = `${pctOfBps(m.net_bps)} per trade on average after costs, over ${count(m.trades)} trades`
  const text =
    typeof m.gross_bps === 'number' && m.gross_bps <= 0
      ? `It lost money even before costs: ${pctOfBps(m.gross_bps)} per trade before costs and ${after}.`
      : `It lost money after costs: ${after}.`
  return {
    text,
    href: '#gate-G1.2',
  }
}

function randomFinding(run: RunRecord): BriefFinding | null {
  const b = run.baseline
  if (!b) return null
  const strategy = b.trade_net_bps ?? run.metrics.net_bps
  const n = count(b.trades ?? run.metrics.trades)
  const matched = b.sampled
    ? `a sample of ${n} matched trades`
    : `the ${n} trades that could be matched`
  if (b.excess_net_bps < 0) {
    return {
      text: `Random entries did better: buying the same stocks on random days for the same holding periods averaged ${pctOfBps(b.net_bps)} per trade, against ${pctOfBps(strategy)} for the strategy, on ${matched}.`,
      href: '#random-entry',
    }
  }
  const t = b.excess_t_stat
  const unclear =
    typeof t === 'number' && Math.abs(t) < 2 ? ', but the margin is not statistically clear' : ''
  return {
    text: `It beat random entries into the same stocks: ${pctOfBps(strategy)} against ${pctOfBps(b.net_bps)} per trade on ${matched}${unclear}.`,
    href: '#random-entry',
  }
}

function oosFinding(run: RunRecord): BriefFinding {
  const ins = run.in_sample?.net_bps
  const oos = run.out_of_sample?.net_bps
  const detail =
    typeof ins === 'number' && typeof oos === 'number'
      ? `it averaged ${pctOfBps(oos)} per trade on the held-back later period, against ${pctOfBps(ins)} on the earlier period`
      : 'its results on the held-back later period fell short of the required level'
  return { text: `It failed the test on unseen data: ${detail}.`, href: '#gate-G3.2' }
}

function failedFinding(row: EffectiveRow): BriefFinding {
  return {
    text: FAILED_TEXT[row.gate] ?? `It failed the ${row.title.toLowerCase()} test.`,
    href: `#gate-${row.gate}`,
  }
}

function caveats(rows: EffectiveRow[]): string[] {
  const out: string[] = []
  const open = rows.filter((r) => r.hard && r.status === 'not_evaluated').length
  if (open > 0) {
    out.push(
      `${open} required test${open === 1 ? ' was' : 's were'} not run. A test that was not run is not a pass.`
    )
  }
  const survivorship = rows.find((r) => r.gate === 'G4.7')
  if (survivorship && /survivor/i.test(survivorship.evidence)) {
    out.push(
      "The stock list is today's F&O list applied to the past, so stocks that later left it are missing (survivorship bias)."
    )
  }
  return out
}

function supporting(detail: RunDetail): string | null {
  const e = detail.evaluations?.diagnostics.find((d) => d.name === 'equal_weight_eligible_universe')
  const s = e?.strategy?.annual_return_pct
  const u = e?.benchmark?.annual_return_pct
  if (typeof s !== 'number' || typeof u !== 'number') return null
  const kept = e?.reproducible ? '' : ' It cannot be reproduced, so it is a historical note only.'
  return `Supporting check, not decisive: on a different, equal-weight portfolio model, holding every eligible stock returned ${pct(u)} a year against ${pct(s)} for the strategy. These figures are not comparable with the portfolio figures above.${kept}`
}

function action(idea: SummaryIdea, detail: RunDetail) {
  if (idea.research_state && STOPPED.includes(idea.research_state)) {
    return { text: 'Do not continue this version.', why: idea.revisit_when ?? '' }
  }
  return {
    text: detail.next_action?.action ?? 'No next action was recorded.',
    why: detail.next_action?.reason ?? '',
  }
}

export function buildDecisionBrief(
  idea: SummaryIdea,
  run: RunRecord,
  detail: RunDetail
): DecisionBriefModel {
  const evaluations = detail.evaluations ?? { corrections: [], portfolio: null, diagnostics: [] }
  const rows = effectiveRows(run.evaluation, evaluations.corrections)
  const portfolio = evaluations.portfolio
  const failed = rows.filter((r) => r.status === 'fail')
  const oosFailed = failed.some((r) => r.gate === 'G3.2')
  const randomBehind = (run.baseline?.excess_net_bps ?? 0) < 0
  const lost = !isProfitable(run, portfolio) || failed.some((r) => r.gate === 'G1.2')

  // Most decisive first: a loss, then the two controls on the selection rule, then hard fails.
  const candidates: BriefFinding[] = []
  if (lost) candidates.push(lossFinding(run))
  const random = randomFinding(run)
  if (random) candidates.push(random)
  if (oosFailed) candidates.push(oosFinding(run))
  const others = failed
    .filter((r) => r.gate !== 'G1.2' && r.gate !== 'G3.2')
    .sort((a, b) => Number(b.hard) - Number(a.hard))
  candidates.push(...others.map(failedFinding))

  const findings = candidates.slice(0, MAX_FINDINGS)
  const failedShown = failed.filter((r) =>
    findings.some((f) => f.href === `#gate-${r.gate}`)
  ).length
  const { text, tone } = headline(idea)
  return {
    headline: text,
    tone,
    reason: reason(run, portfolio, failed, randomBehind || oosFailed),
    figures: figures(run, portfolio),
    basis: basis(run, portfolio),
    portfolio,
    findings,
    moreFailed: Math.max(0, failed.length - failedShown),
    supporting: supporting(detail),
    caveats: caveats(rows),
    action: action(idea, detail),
  }
}
