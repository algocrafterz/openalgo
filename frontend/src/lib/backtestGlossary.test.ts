import { describe, expect, it } from 'vitest'
import { GLOSSARY, GLOSSARY_IDS, getTerm } from './backtestGlossary'

// Terms required by BACKTESTS-UI-TECHNICAL-REQUIREMENTS.md section 2.15.
const REQUIRED_2_15 = [
  'Backtest',
  'Strategy Version',
  'Backtest Run',
  'Dataset',
  'In-Sample',
  'Out-of-Sample',
  'Walk-Forward',
  'Expectancy',
  'Profit Factor',
  'Maximum Drawdown',
  'Sharpe Ratio',
  'Slippage',
  'Transaction Costs',
  'Robustness',
  'Parameter Sensitivity',
  'Regime',
  'Overfitting',
  'Verdict',
  'Promotion Gate',
]

const REQUIRED_IDS = [
  'edge',
  'marginal',
  'no_edge',
  'inconclusive',
  'run_verdict',
  'research_state',
  'stage',
  'promotion_decision',
  'ready_for_next_stage',
  'not_ready',
  'blocked',
  'not_run',
  'verdict_mapping',
  'category',
  'tags',
  'strategy_version',
  'run',
  'strategy_change',
  'parameter_change',
  'execution_change',
  'cost_change',
  'universe_change',
  'deployment_profile_change',
  'dataset_change',
  'data_range_change',
  'engine_change',
  'research_rerun',
  'reproducible_run',
  'fingerprint',
  'source_user',
  'source_derived',
  'source_platform_default',
  'source_external',
  'source_inferred',
  'materiality',
  'ledger',
  'data_quality',
  'validity_checks',
  'lookahead_check',
  'lineage',
  'exclusion',
  'reconciliation',
  'trial_ledger',
  'cost_components',
  'trade_subsets',
]

const EMOJI = /[\u{1F000}-\u{1FFFF}\u{2190}-\u{21FF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}]/u

describe('GLOSSARY', () => {
  it('has non-empty short and long text for every term', () => {
    for (const id of GLOSSARY_IDS) {
      const t = GLOSSARY[id]
      expect(t.id).toBe(id)
      expect(t.term.trim()).not.toBe('')
      expect(t.short.trim().length).toBeGreaterThan(10)
      expect(t.long.trim().length).toBeGreaterThan(t.short.trim().length)
      expect(t.category.trim()).not.toBe('')
    }
  })

  it('has unique ids and unique term names', () => {
    expect(new Set(GLOSSARY_IDS).size).toBe(GLOSSARY_IDS.length)
    const names = GLOSSARY_IDS.map((id) => GLOSSARY[id].term)
    expect(new Set(names).size).toBe(names.length)
  })

  it('defines every term from requirements 2.15', () => {
    const names = new Set(GLOSSARY_IDS.map((id) => GLOSSARY[id].term))
    for (const name of REQUIRED_2_15) expect(names.has(name)).toBe(true)
  })

  it('defines the verdict, lifecycle, change class and ledger terms', () => {
    const ids = new Set<string>(GLOSSARY_IDS)
    for (const id of REQUIRED_IDS) expect(ids.has(id)).toBe(true)
  })

  it('contains no emoji or icon characters', () => {
    for (const id of GLOSSARY_IDS) {
      const t = GLOSSARY[id]
      expect(EMOJI.test(`${t.term} ${t.short} ${t.long} ${t.category}`)).toBe(false)
    }
  })

  it('states that verdicts are calculated', () => {
    for (const id of ['edge', 'marginal', 'no_edge', 'inconclusive', 'run_verdict'] as const) {
      expect(`${GLOSSARY[id].short} ${GLOSSARY[id].long}`.toLowerCase()).toContain('calculated')
    }
  })

  it('says PROMISING and ROBUST are not verdicts', () => {
    const t = GLOSSARY.verdict_mapping
    expect(t.long).toContain('PROMISING')
    expect(t.long).toContain('ROBUST')
    expect(t.long).toContain('not verdicts')
  })

  it('says category and tags create no new version', () => {
    expect(GLOSSARY.tags.long.toLowerCase()).toContain('no new version')
    expect(GLOSSARY.category.long.toLowerCase()).toContain('no new version')
  })

  it('getTerm returns the entry or undefined', () => {
    expect(getTerm('edge')?.term).toBe('EDGE')
    expect(getTerm('nope')).toBeUndefined()
    expect(getTerm(undefined)).toBeUndefined()
  })
})
