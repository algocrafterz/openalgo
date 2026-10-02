import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { RunFingerprints } from '@/api/backtestResults'
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
