import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { RunFingerprints } from '@/api/backtestResults'
import REAL_SPEC from './fixtures/idea029-v4-frozen-spec.json'
import { StrategyDefinition } from './StrategyDefinition'

const FPS: RunFingerprints = {
  rules: 'rules0001aaaaaaaa',
  params: 'params002bbbbbbb',
  strategy: 'strat0003cccccccc',
  execution: 'x',
  cost: 'x',
  universe: 'x',
  profile: 'x',
  dataset: 'x',
}

const CURRENT = { name: 'Current idea', entry: 'CURRENT ENTRY RULE', parameters: { n: 99 } }
const FROZEN = {
  name: 'Frozen idea',
  entry: ['FROZEN ENTRY RULE'],
  exit: 'FROZEN EXIT',
  parameters: { atr_mult: 1.5 },
}

describe('StrategyDefinition frozen', () => {
  it('renders the frozen spec rather than the current spec', () => {
    render(
      <StrategyDefinition
        spec={CURRENT}
        frozen={{
          spec: FROZEN,
          version: 'v3',
          parentVersion: 'v2',
          branchLabel: 'tighter-stop',
          fingerprints: FPS,
        }}
      />
    )
    const panel = screen.getByTestId('strategy-definition')
    expect(panel).toHaveTextContent('Frozen idea')
    expect(panel).toHaveTextContent('FROZEN ENTRY RULE')
    expect(panel).toHaveTextContent('atr_mult = 1.5')
    expect(panel).not.toHaveTextContent('CURRENT ENTRY RULE')
    expect(panel).not.toHaveTextContent('n = 99')
    expect(panel).toHaveTextContent('Frozen with this run')
  })

  it('shows version, parent, branch label and short fingerprints with full titles', () => {
    render(
      <StrategyDefinition
        spec={null}
        frozen={{
          spec: FROZEN,
          version: 'v3',
          parentVersion: 'v2',
          branchLabel: 'tighter-stop',
          fingerprints: FPS,
        }}
      />
    )
    const meta = screen.getByTestId('frozen-meta')
    expect(meta).toHaveTextContent('v3')
    expect(meta).toHaveTextContent('v2')
    expect(meta).toHaveTextContent('tighter-stop')
    expect(screen.getByText('rules000')).toHaveAttribute('title', FPS.rules)
    expect(screen.getByText('params00')).toHaveAttribute('title', FPS.params)
    expect(screen.getByText('strat000')).toHaveAttribute('title', FPS.strategy)
  })

  it('shows rules and parameters as separate sections', () => {
    render(
      <StrategyDefinition
        spec={null}
        frozen={{
          spec: FROZEN,
          version: null,
          parentVersion: null,
          branchLabel: null,
          fingerprints: null,
        }}
      />
    )
    expect(screen.getByTestId('spec-rules')).toHaveTextContent('FROZEN ENTRY RULE')
    expect(screen.getByTestId('spec-rules')).not.toHaveTextContent('atr_mult')
    expect(screen.getByTestId('spec-parameters')).toHaveTextContent('atr_mult = 1.5')
    expect(screen.getByTestId('spec-parameters')).not.toHaveTextContent('FROZEN ENTRY RULE')
  })

  it('falls back to the current spec when frozen is absent, but not when captured without a spec', () => {
    const { rerender } = render(<StrategyDefinition spec={CURRENT} />)
    expect(screen.getByTestId('strategy-definition')).toHaveTextContent('CURRENT ENTRY RULE')
    expect(screen.queryByText('Frozen with this run')).not.toBeInTheDocument()
    rerender(
      <StrategyDefinition
        spec={CURRENT}
        frozen={{
          spec: null,
          version: null,
          parentVersion: null,
          branchLabel: null,
          fingerprints: null,
        }}
      />
    )
    expect(screen.queryByTestId('strategy-definition')).not.toBeInTheDocument()
    expect(screen.getByTestId('frozen-spec-missing')).toBeInTheDocument()
    expect(screen.queryByText('Frozen with this run')).not.toBeInTheDocument()
  })
})

describe('StrategyDefinition provenance', () => {
  const empty = {
    spec: null,
    version: 'v1',
    parentVersion: null,
    branchLabel: null,
    fingerprints: null,
  }

  it('says the frozen spec was not captured instead of showing the current spec', () => {
    render(<StrategyDefinition spec={CURRENT} frozen={empty} />)
    expect(screen.getByTestId('frozen-spec-missing')).toHaveTextContent(
      'Frozen spec not captured for this run'
    )
    expect(screen.queryByTestId('strategy-definition')).not.toBeInTheDocument()
    expect(screen.queryByText(/CURRENT ENTRY RULE/)).not.toBeInTheDocument()
  })

  it('shows a skeleton, not the current spec, while the run detail loads', () => {
    render(<StrategyDefinition spec={CURRENT} loading />)
    expect(screen.getByTestId('strategy-definition-loading')).toBeInTheDocument()
    expect(screen.queryByText(/CURRENT ENTRY RULE/)).not.toBeInTheDocument()
  })

  it('labels the current spec fallback used for a legacy run', () => {
    render(<StrategyDefinition spec={CURRENT} />)
    expect(screen.getByTestId('current-spec-label')).toHaveTextContent(
      'Current idea spec, not necessarily the one this run used'
    )
    expect(screen.getByTestId('strategy-definition')).toHaveTextContent('CURRENT ENTRY RULE')
    expect(screen.queryByText('Frozen with this run')).not.toBeInTheDocument()
  })

  it('tolerates partial fingerprints', () => {
    render(
      <StrategyDefinition
        spec={null}
        frozen={{
          ...empty,
          spec: FROZEN,
          fingerprints: { rules: 'abcdef0123' } as unknown as RunFingerprints,
        }}
      />
    )
    expect(screen.getByTestId('frozen-meta')).toHaveTextContent('not captured')
  })
})

const frozenOf = (spec: Record<string, unknown>) => ({
  spec,
  version: 'v1',
  parentVersion: null,
  branchLabel: null,
  fingerprints: FPS,
})

describe('StrategyDefinition with the real IDEA-029 v4 frozen spec', () => {
  // The fixture is a copy of runs/2026-10-09-vwap-2sd-range-mean-reversion-v4/strategy/spec.yaml;
  // signal_engine/tests/test_backtests_ui_spec_fixture.py fails if the two drift apart.
  const renderReal = () => {
    render(<StrategyDefinition spec={null} frozen={frozenOf(REAL_SPEC)} />)
    return screen.getByTestId('strategy-definition')
  }

  it('shows the name and every rule the spec holds', () => {
    const panel = renderReal()
    expect(panel).toHaveTextContent('VWAP 2SD range mean reversion')
    expect(panel).toHaveTextContent('every rule below reads completed bars only')
    expect(panel).toHaveTextContent('Target is the live session VWAP')
    expect(panel).toHaveTextContent('Session VWAP, recomputed each bar')
  })

  it('shows the stop rule under stop', () => {
    const panel = renderReal()
    expect(panel).toHaveTextContent('Stop loss')
    expect(panel).toHaveTextContent('Long stop = extreme - 0.20 x ATR14_5m')
  })

  it('shows sizing, holding period, re-entry, timing and the entry window', () => {
    const panel = renderReal()
    expect(panel).toHaveTextContent('Position sizing')
    expect(panel).toHaveTextContent('basis fixed_notional')
    expect(panel).toHaveTextContent('capital per trade 100000')
    expect(panel).toHaveTextContent('risk per trade pct 1')
    expect(panel).toHaveTextContent('Intraday only; every position is closed by 14:45')
    expect(panel).toHaveTextContent('allowed true')
    expect(panel).toHaveTextContent('max per symbol day 2')
    expect(panel).toHaveTextContent('fill model next_bar_open')
    expect(panel).toHaveTextContent('signal evaluation bar_close')
    expect(panel).toHaveTextContent('entry 09:20-14:15')
  })

  it('shows style, direction and timeframe', () => {
    const panel = renderReal()
    expect(panel).toHaveTextContent('Styleintraday')
    expect(panel).toHaveTextContent('Directionboth')
    expect(panel).toHaveTextContent('Timeframe5m')
    expect(panel).toHaveTextContent('Filtersnone')
  })

  it('renders each structured indicator as text, never as an object', () => {
    const panel = renderReal()
    expect(panel).not.toHaveTextContent('[object Object]')
    expect(panel).toHaveTextContent('VWAP (anchor session, source hlc3)')
    expect(panel).toHaveTextContent('EMA (period 9, source close)')
    expect(panel).toHaveTextContent('EMA_15M (period 20, source close)')
    expect(panel).toHaveTextContent('ADX_15M (period 14, source close)')
  })

  it('lists every parameter', () => {
    const panel = renderReal()
    const params = REAL_SPEC.parameters as Record<string, unknown>
    expect(Object.keys(params)).toHaveLength(13)
    for (const [k, v] of Object.entries(params)) {
      expect(panel).toHaveTextContent(`${k} = ${JSON.stringify(v)}`)
    }
  })
})

describe('StrategyDefinition legacy and mixed keys', () => {
  it('still renders the legacy key names', () => {
    render(
      <StrategyDefinition
        spec={null}
        frozen={frozenOf({
          name: 'Legacy',
          entry: 'E',
          stop_loss: 'LEGACY STOP',
          position_sizing: 'LEGACY SIZE',
          max_holding_period: 'LEGACY HOLD',
          session_filters: 'LEGACY SESSION',
          trading_style: 'LEGACY STYLE',
          indicators: ['RSI(14)', 'EMA(20)'],
        })}
      />
    )
    const panel = screen.getByTestId('strategy-definition')
    for (const text of [
      'LEGACY STOP',
      'LEGACY SIZE',
      'LEGACY HOLD',
      'LEGACY SESSION',
      'LEGACY STYLE',
      'RSI(14), EMA(20)',
    ]) {
      expect(panel).toHaveTextContent(text)
    }
  })

  it('prefers the current key when both are present', () => {
    render(
      <StrategyDefinition
        spec={null}
        frozen={frozenOf({ name: 'Both', stop: 'NEW STOP', stop_loss: 'OLD STOP' })}
      />
    )
    const panel = screen.getByTestId('strategy-definition')
    expect(panel).toHaveTextContent('NEW STOP')
    expect(panel).not.toHaveTextContent('OLD STOP')
  })

  it('shows market when the spec holds one and omits absent fields', () => {
    render(<StrategyDefinition spec={null} frozen={frozenOf({ name: 'M', market: 'NSE cash' })} />)
    const panel = screen.getByTestId('strategy-definition')
    expect(panel).toHaveTextContent('MarketNSE cash')
    expect(panel).not.toHaveTextContent('Stop loss')
    expect(panel).not.toHaveTextContent('Re-entry')
  })

  it('hides a field whose value is an empty structure instead of showing a blank row', () => {
    render(
      <StrategyDefinition
        spec={null}
        frozen={frozenOf({ name: 'Empty', re_entry: {}, timing: [], stop: ['STOP TEXT'] })}
      />
    )
    const panel = screen.getByTestId('strategy-definition')
    expect(panel).toHaveTextContent('STOP TEXT')
    expect(panel).not.toHaveTextContent('Re-entry')
    expect(panel).not.toHaveTextContent('Timing')
  })
})
