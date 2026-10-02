import { render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { AssumptionEntry } from '@/api/backtestResults'
import { makeRun } from '@/test/backtestResultsFixtures'
import { RunAssumptions } from './RunAssumptions'

vi.mock('./InfoTip', () => ({ InfoTip: ({ term }: { term: string }) => <span>{term}</span> }))

function entry(over: Partial<AssumptionEntry>): AssumptionEntry {
  return {
    id: 'a1',
    field: 'slippage',
    section: 'execution',
    value: 5,
    source: 'user',
    confidence: 'high',
    materiality: 'medium',
    reason: 'because',
    user_confirmed: true,
    ...over,
  }
}

const ENTRIES: AssumptionEntry[] = [
  entry({ id: 'u', field: 'direction', value: 'long_only', source: 'user', reason: 'you said so' }),
  entry({ id: 'd', field: 'lot_size', value: 25, source: 'derived', reason: 'from symbol master' }),
  entry({ id: 'p', field: 'fill_model', value: 'next_open', source: 'platform_default' }),
  entry({ id: 'm', field: 'tick_size', value: 0.05, source: 'market_data' }),
  entry({ id: 'b', field: 'margin', value: 0.2, source: 'broker_config' }),
  entry({
    id: 'i1',
    field: 'stop_loss',
    value: '1.5 ATR',
    source: 'inferred',
    materiality: 'high',
    user_confirmed: false,
    confidence: 'low',
    reason: 'guessed from text',
  }),
  entry({
    id: 'i2',
    field: 'timeframe',
    value: '5m',
    source: 'inferred',
    materiality: 'high',
    user_confirmed: true,
  }),
  entry({
    id: 'i3',
    field: 'minor',
    value: 1,
    source: 'inferred',
    materiality: 'low',
    user_confirmed: false,
  }),
]

describe('RunAssumptions with resolved assumptions', () => {
  const props = { sealed_ist: '2026-10-01 09:00', entries: ENTRIES }

  it('groups entries by source with External merging market data and broker config', () => {
    render(<RunAssumptions run={makeRun({})} assumptions={props} />)
    const user = screen.getByTestId('assumption-group-user')
    expect(within(user).getByText('Stated by you')).toBeInTheDocument()
    expect(user).toHaveTextContent('direction')
    expect(user).toHaveTextContent('long_only')
    expect(user).toHaveTextContent('you said so')
    expect(screen.getByTestId('assumption-group-derived')).toHaveTextContent('lot_size')
    expect(screen.getByTestId('assumption-group-platform_default')).toHaveTextContent('fill_model')
    const ext = screen.getByTestId('assumption-group-external')
    expect(within(ext).getByText('External')).toBeInTheDocument()
    expect(ext).toHaveTextContent('tick_size')
    expect(ext).toHaveTextContent('margin')
    const inferred = screen.getByTestId('assumption-group-inferred')
    expect(within(inferred).getByText('Inferred (needs your review)')).toBeInTheDocument()
  })

  it('shows confidence, materiality and confirmation state', () => {
    render(<RunAssumptions run={makeRun({})} assumptions={props} />)
    const row = screen.getByTestId('assumption-u')
    expect(row).toHaveTextContent('Confirmed')
    expect(row).toHaveTextContent(/high/i)
    expect(row).toHaveTextContent(/medium/i)
    expect(screen.getByTestId('assumption-i1')).toHaveTextContent('Not confirmed')
  })

  it('highlights only high-materiality inferred entries that are not confirmed', () => {
    render(<RunAssumptions run={makeRun({})} assumptions={props} />)
    expect(screen.getByTestId('assumption-i1')).toHaveTextContent('Unresolved')
    expect(screen.getByTestId('assumption-i2')).not.toHaveTextContent('Unresolved')
    expect(screen.getByTestId('assumption-i3')).not.toHaveTextContent('Unresolved')
    expect(screen.getByTestId('assumption-u')).not.toHaveTextContent('Unresolved')
  })

  it('shows the sealed time', () => {
    render(<RunAssumptions run={makeRun({})} assumptions={props} />)
    expect(screen.getByTestId('assumptions-sealed')).toHaveTextContent('2026-10-01 09:00')
  })

  it('omits empty groups', () => {
    render(
      <RunAssumptions
        run={makeRun({})}
        assumptions={{ sealed_ist: 's', entries: [entry({ source: 'user' })] }}
      />
    )
    expect(screen.queryByTestId('assumption-group-inferred')).not.toBeInTheDocument()
  })
})

describe('RunAssumptions legacy fallback', () => {
  it('renders the engine defaults text when assumptions is null', () => {
    render(<RunAssumptions run={makeRun({})} assumptions={null} />)
    expect(screen.getByText('Assumptions and provenance')).toBeInTheDocument()
    expect(screen.getByText('Cost model')).toBeInTheDocument()
    expect(screen.getByText('Parameters')).toBeInTheDocument()
    expect(screen.queryByTestId('assumptions-sealed')).not.toBeInTheDocument()
  })

  it('still works with only the old run prop', () => {
    render(<RunAssumptions run={makeRun({})} />)
    expect(screen.getByText('Round-trip cost')).toBeInTheDocument()
  })
})
