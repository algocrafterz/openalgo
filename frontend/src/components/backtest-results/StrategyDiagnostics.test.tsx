import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { makeRun } from '@/test/backtestResultsFixtures'
import { StrategyDiagnostics } from './StrategyDiagnostics'

const g = (label: string, mean: number) => ({
  label,
  trades: 10,
  net_bps_mean: mean,
  net_bps_total: mean * 10,
  win_rate: 0.5,
})

describe('StrategyDiagnostics', () => {
  it('shows each split from the stored trades', () => {
    render(
      <StrategyDiagnostics
        run={makeRun()}
        diagnostics={{
          trades: 100,
          symbols_total: 3,
          symbols_best: [g('INFY', 12)],
          symbols_worst: [g('SBIN', -9)],
          by_weekday: [g('Monday', 3)],
          by_entry_hour: [g('10:00', -2)],
          by_exit_reason: [g('SL', -20)],
          by_side: [g('BUY', 1)],
        }}
      />
    )
    expect(within(screen.getByTestId('diag-best')).getByText('INFY')).toBeInTheDocument()
    expect(within(screen.getByTestId('diag-worst')).getByText('SBIN')).toBeInTheDocument()
    expect(within(screen.getByTestId('diag-weekday')).getByText('Monday')).toBeInTheDocument()
    expect(within(screen.getByTestId('diag-hour')).getByText('10:00')).toBeInTheDocument()
    expect(within(screen.getByTestId('diag-exit')).getByText('SL')).toBeInTheDocument()
    expect(within(screen.getByTestId('diag-side')).getByText('BUY')).toBeInTheDocument()
  })

  it('says so when the trade list is not stored and there is no regime split', () => {
    render(<StrategyDiagnostics run={makeRun({ regimes: undefined })} diagnostics={null} />)
    expect(screen.getByTestId('diagnostics-unavailable')).toBeInTheDocument()
    expect(screen.getByTestId('regimes-unavailable')).toHaveTextContent('not available')
  })
})
