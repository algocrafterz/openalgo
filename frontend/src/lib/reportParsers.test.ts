import { describe, expect, it } from 'vitest'
import { parseExecutionHealth, parseStrategyPnl } from './reportParsers'

const EXEC = `# Execution health 2026-11-03

Overall: **REGRESSION**  (cap check: OK)

| Metric | Today | Status | Prev day | Baseline | Watch / Regress |
| --- | --- | --- | --- | --- | --- |
| Limiter refused calls (total) | 3575 | REGRESSION | 5193 | 0 | >0 / >5 |
| Limiter timeouts (total) | 0 | OK | 0 | 0 | >0 / >5 |

Sources: x
`

const EOD = `## Strategy P&L

\`\`\`
Estimated charges
  BREAKINGTRADE                gross      229.8   charges      58.7    25.5% of profit   after charges      171.1
  EVWAP-EARLY-PY               gross   -8,672.3   charges   1,597.6    no gross profit   after charges  -10,269.9
  ALL STRATEGIES               gross   -8,204.1   charges   1,721.4    no gross profit   after charges   -9,925.6
    broker Flattrade: 45 trades, charges 1,721.4

  realised R       mean -0.201   median -0.076   win 37.8%
RESULT: PASS
\`\`\`
`

describe('parseExecutionHealth', () => {
  it('reads the overall verdict and every metric row', () => {
    const r = parseExecutionHealth(EXEC)
    expect(r?.overall).toBe('REGRESSION')
    expect(r?.rows).toEqual([
      {
        metric: 'Limiter refused calls (total)',
        today: '3575',
        status: 'REGRESSION',
        prev: '5193',
      },
      { metric: 'Limiter timeouts (total)', today: '0', status: 'OK', prev: '0' },
    ])
  })
  it('returns null when there is no metric table', () => {
    expect(parseExecutionHealth('# nothing here')).toBeNull()
  })
  it('tolerates a missing previous-day column', () => {
    const r = parseExecutionHealth(
      '| Metric | Today | Status |\n| --- | --- | --- |\n| A | 1 | OK |\n'
    )
    expect(r?.rows[0]).toEqual({ metric: 'A', today: '1', status: 'OK', prev: undefined })
  })
})

describe('parseStrategyPnl', () => {
  it('reads strategies, the total, trades, win rate and reconciliation as printed', () => {
    const r = parseStrategyPnl(EOD)
    expect(r?.strategies).toEqual([
      { name: 'BREAKINGTRADE', gross: 229.8, charges: 58.7, net: 171.1 },
      { name: 'EVWAP-EARLY-PY', gross: -8672.3, charges: 1597.6, net: -10269.9 },
    ])
    expect(r?.total).toEqual({
      name: 'ALL STRATEGIES',
      gross: -8204.1,
      charges: 1721.4,
      net: -9925.6,
    })
    expect(r?.trades).toBe(45)
    expect(r?.winRatePct).toBe(37.8)
    expect(r?.reconciliation).toBe('PASS')
  })
  it('leaves unprinted values undefined', () => {
    const r = parseStrategyPnl(
      '  ORB   gross  -178.1   charges   31.6   no gross profit   after charges  -209.7'
    )
    expect(r?.trades).toBeUndefined()
    expect(r?.winRatePct).toBeUndefined()
    expect(r?.total).toBeUndefined()
  })
  it('returns null when the report has no charges block', () => {
    expect(parseStrategyPnl('## Ledger\nnothing')).toBeNull()
  })
})
