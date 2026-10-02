import { render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { RunScope } from '@/api/backtestResults'
import { BacktestScope } from './BacktestScope'

vi.mock('./InfoTip', () => ({ InfoTip: ({ term }: { term: string }) => <span>{term}</span> }))

const SHA = 'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789'
const FP = '1234567890abcdef1234567890abcdef'

function makeScope(over: Partial<RunScope> = {}): RunScope {
  return {
    market: 'NSE equity',
    universe: 'fno-208',
    symbol_count: 208,
    style: 'intraday',
    direction: 'long_only',
    timeframe: '5m',
    product: 'MIS',
    period: { start: '2016-10-03', end: '2026-09-25' },
    strategy_window: { entry: '09:30-11:00', no_trade: ['12:00-13:00'] },
    exchange_session: '09:15-15:30',
    broker_cutoff: '15:15',
    force_flat: '15:20',
    capital_per_trade: 50000,
    risk_per_trade_pct: 1,
    portfolio_mode: 'concurrent',
    portfolio_scope: 'shared pool',
    slippage_bps_per_side: 5,
    cost_bps_round_trip: 9.4,
    profile: { name: 'zerodha-mis', sha256: SHA },
    dataset: {
      id: 'ds-1',
      source: 'openalgo',
      interval: '5m',
      data_as_of: '2026-09-29',
      fingerprint: FP,
    },
    ...over,
  }
}

describe('BacktestScope', () => {
  it('shows one note when scope was not captured', () => {
    render(<BacktestScope scope={null} />)
    expect(screen.getByText('Not captured for this run')).toBeInTheDocument()
    expect(screen.queryByText('Execution')).not.toBeInTheDocument()
  })

  it('renders every labelled group with its values', () => {
    render(<BacktestScope scope={makeScope()} />)
    const box = screen.getByTestId('backtest-scope')
    for (const g of [
      'Market and universe',
      'Strategy',
      'Period',
      'Sessions',
      'Execution',
      'Deployment profile',
      'Dataset',
    ]) {
      expect(within(box).getByText(g)).toBeInTheDocument()
    }
    expect(box).toHaveTextContent('fno-208')
    expect(box).toHaveTextContent('208')
    expect(box).toHaveTextContent('long_only')
    expect(box).toHaveTextContent('MIS')
    expect(box).toHaveTextContent('2016-10-03 to 2026-09-25')
    expect(box).toHaveTextContent('5 bps')
    expect(box).toHaveTextContent('9.4 bps')
    expect(box).toHaveTextContent('zerodha-mis')
    expect(box).toHaveTextContent('ds-1')
    expect(box).toHaveTextContent('2026-09-29')
  })

  it('keeps strategy window, exchange session, broker cutoff and force-flat on separate lines', () => {
    render(<BacktestScope scope={makeScope()} />)
    expect(screen.getByTestId('scope-strategy-window')).toHaveTextContent('09:30-11:00')
    expect(screen.getByTestId('scope-exchange-session')).toHaveTextContent('09:15-15:30')
    expect(screen.getByTestId('scope-broker-cutoff')).toHaveTextContent('15:15')
    expect(screen.getByTestId('scope-force-flat')).toHaveTextContent('15:20')
    expect(screen.getByTestId('scope-broker-cutoff')).not.toHaveTextContent('15:20')
  })

  it('reads not captured for null values', () => {
    render(
      <BacktestScope
        scope={makeScope({
          broker_cutoff: null,
          force_flat: null,
          profile: null,
          dataset: null,
          slippage_bps_per_side: null,
        })}
      />
    )
    expect(screen.getByTestId('scope-broker-cutoff')).toHaveTextContent('not captured')
    expect(screen.getByTestId('scope-force-flat')).toHaveTextContent('not captured')
    expect(screen.getByTestId('scope-profile-name')).toHaveTextContent('not captured')
    expect(screen.getByTestId('scope-dataset-id')).toHaveTextContent('not captured')
  })

  it('shows short hashes with the full value in a title', () => {
    render(<BacktestScope scope={makeScope()} />)
    const profile = screen.getByText(SHA.slice(0, 8))
    expect(profile).toHaveAttribute('title', SHA)
    const ds = screen.getByText(FP.slice(0, 8))
    expect(ds).toHaveAttribute('title', FP)
  })
})

describe('BacktestScope partial data', () => {
  it('renders not captured for a profile hash or dataset fingerprint that is missing', () => {
    render(
      <BacktestScope
        scope={makeScope({
          profile: { name: 'p' } as unknown as RunScope['profile'],
          dataset: { id: 'd' } as unknown as RunScope['dataset'],
        })}
      />
    )
    const card = screen.getByTestId('backtest-scope')
    expect(card).toHaveTextContent('not captured')
  })
})
