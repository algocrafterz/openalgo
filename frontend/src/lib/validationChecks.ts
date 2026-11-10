import type { EvaluationBlock, GateCorrection, Promotion } from '@/api/backtestResults'
import { type EffectiveRow, effectiveRows } from './runSummary'

/**
 * The checks behind a verdict, grouped into the questions a trader asks. This only groups and
 * counts the gate results signal_engine recorded: it sets no threshold and decides nothing.
 * A group's state is the worst of its gates' recorded statuses, and a gate that was not run is
 * never counted as a pass.
 */
export type CheckState = 'failed' | 'not_run' | 'passed' | 'reported' | 'not_applicable'

export interface CheckGroup {
  id: string
  label: string
  state: CheckState
  /** One plain line: what failed, or what was not run, or how many passed. */
  summary: string
  /** A failed gate here is one that decides the verdict. */
  decidesVerdict: boolean
  rows: EffectiveRow[]
}

interface GroupDef {
  id: string
  label: string
  /** Matches a gate id such as `G1.2`. */
  match: RegExp
}

/** Order is the order a trader reads them. A gate that matches none lands in "Other checks". */
const GROUPS: GroupDef[] = [
  { id: 'edge', label: 'Edge after costs', match: /^G1\./ },
  { id: 'random', label: 'Random-entry comparison', match: /^X\.4$/ },
  { id: 'oos', label: 'Out-of-sample performance', match: /^G3\./ },
  { id: 'robustness', label: 'Robustness', match: /^(G4\.|G5\.1$|X\.[123]$)/ },
  { id: 'sample', label: 'Sample sufficiency', match: /^G2\./ },
  { id: 'execution', label: 'Execution assumptions', match: /^(G5\.[2-5]$|G6\.)/ },
  { id: 'data', label: 'Data integrity', match: /^G0\./ },
]

const STATE_LABEL: Record<CheckState, string> = {
  failed: 'Failed',
  not_run: 'Not fully checked',
  passed: 'Passed',
  reported: 'Reported only',
  not_applicable: 'Not applicable',
}

export const checkStateLabel = (s: CheckState): string => STATE_LABEL[s]

/** A gate numbered G0 to G6 is a backtest gate by number (G7 and G8 are paper, G9 is live). */
const BACKTEST_NUMBERED = /^G[0-6]\./

/**
 * The gates that answer whether a strategy is ready to run, not whether the backtest is sound,
 * by the stage the run's own promotion record lists them under. The backtest checks leave them
 * out. Two backend rules decide the edge cases, and neither is a guess:
 *
 * - A gate the record files under the backtest stage stays a backtest check, even when its class
 *   is "deployment" (the square-off gate G6.9 decides the verdict). Runs sealed before the
 *   2026-10-09 override list G4.8 and G6.6 there, and are read as recorded.
 * - The paper-exempt gates (G0.7 and G2.4) are listed under `live`, because the backtest-to-paper
 *   step does not require them to have run. They remain part of the backtest verdict (a missing
 *   one still holds EDGE), so a G0 to G6 gate under `live` is not readiness. The record carries
 *   no flag for this; the rule follows `PAPER_EXEMPT_GATES` in signal_engine/backtest/promotion.py.
 */
export function readinessGateIds(promotion: Promotion | null | undefined): Set<string> {
  const ids = new Set<string>()
  for (const g of promotion?.stages?.paper?.gates ?? []) ids.add(g.gate)
  for (const g of promotion?.stages?.live?.gates ?? []) {
    if (!BACKTEST_NUMBERED.test(g.gate)) ids.add(g.gate)
  }
  return ids
}

function names(rows: EffectiveRow[], max = 2): string {
  const shown = rows.slice(0, max).map((r) => r.title)
  const more = rows.length - shown.length
  return more > 0 ? `${shown.join(', ')} and ${more} more` : shown.join(', ')
}

function roll(def: { id: string; label: string }, rows: EffectiveRow[]): CheckGroup {
  const failed = rows.filter((r) => r.status === 'fail')
  const notRun = rows.filter((r) => r.status === 'not_evaluated')
  const passed = rows.filter((r) => r.status === 'pass')
  const reported = rows.filter((r) => r.status === 'report')
  let state: CheckState
  let summary: string
  if (failed.length > 0) {
    state = 'failed'
    summary = `${failed.length} failed: ${names(failed)}.`
    if (notRun.length > 0) summary += ` ${notRun.length} not run.`
  } else if (notRun.length > 0) {
    state = 'not_run'
    summary = `${notRun.length} not run: ${names(notRun)}. A test that was not run is not a pass.`
  } else if (passed.length > 0) {
    state = 'passed'
    summary =
      reported.length > 0
        ? `${passed.length} passed, ${reported.length} reported only.`
        : `${passed.length} check${passed.length === 1 ? '' : 's'} passed.`
  } else if (reported.length > 0) {
    state = 'reported'
    summary = `${reported.length} figure${reported.length === 1 ? '' : 's'} reported, nothing to pass or fail.`
  } else {
    state = 'not_applicable'
    summary = 'Does not apply to this run.'
  }
  return {
    id: def.id,
    label: def.label,
    state,
    summary,
    decidesVerdict: failed.some((r) => r.hard),
    rows,
  }
}

/**
 * The backtest checks for one run, grouped. Paper and live gates are not in these groups; the
 * caller shows them separately. Groups with no gate in this run are dropped.
 */
export function validationChecks(
  evaluation: EvaluationBlock | null | undefined,
  corrections: GateCorrection[] | undefined,
  promotion: Promotion | null | undefined,
  /**
   * The run's recorded random-entry comparison in words. It stands in for the random-entry
   * check when this run has no gate row for it, so that check never vanishes from the list.
   */
  randomEntryNote: string | null = null
): CheckGroup[] {
  const readiness = readinessGateIds(promotion)
  const rows = effectiveRows(evaluation, corrections ?? []).filter((r) => !readiness.has(r.gate))
  const taken = new Set<string>()
  const groups: CheckGroup[] = []
  for (const def of GROUPS) {
    const inGroup = rows.filter((r) => def.match.test(r.gate))
    for (const r of inGroup) taken.add(r.gate)
    if (inGroup.length > 0) groups.push(roll(def, inGroup))
  }
  if (randomEntryNote && !groups.some((g) => g.id === 'random')) {
    const at = groups.findIndex((g) => g.id === 'edge') + 1
    groups.splice(at, 0, {
      id: 'random',
      label: 'Random-entry comparison',
      state: 'reported',
      summary: randomEntryNote,
      decidesVerdict: false,
      rows: [],
    })
  }
  // A future gate that no group claims must not vanish from the summary.
  const other = rows.filter((r) => !taken.has(r.gate))
  if (other.length > 0) groups.push(roll({ id: 'other', label: 'Other checks' }, other))
  return groups
}
