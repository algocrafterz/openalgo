import { describe, expect, it, vi } from 'vitest'
import { emptyToday, todayClosed } from '@/test/discretionaryFixtures'
import { render, screen, userEvent, within } from '@/test/test-utils'
import { TodayPerformance } from './TodayPerformance'

const money = (v: number) => `Rs ${v.toFixed(2)}`

/** The big number of the card with this label inside the Today section. */
function cardValue(label: string): string {
  const section = screen.getByTestId('discretionary-today')
  const el = within(section).getByText(label, { selector: 'div' })
  return el.parentElement?.querySelector('.font-mono')?.textContent ?? ''
}

const grace = (extra = {}) => ({
  id: 7,
  broker: 'flattrade',
  account: 'A1',
  symbol: 'SBIN',
  exchange: 'NSE',
  product: 'MIS',
  side: 'SELL',
  qty: 45,
  price: 105,
  fill_time: '2026-11-17 10:50:00',
  attributable_from: '2026-11-17 11:20:00',
  due: false,
  affects_open_position: true,
  ...extra,
})

describe('TodayPerformance', () => {
  it('shows zeros and the empty text when nothing happened today', () => {
    render(<TodayPerformance today={emptyToday()} formatCurrency={money} />)
    expect(screen.getByText("Today's Performance")).toBeInTheDocument()
    expect(cardValue('Realised (gross)')).toBe('Rs 0.00')
    expect(cardValue('Net after est. charges')).toBe('Rs 0.00')
    expect(cardValue('Unrealised')).toBe('—')
    expect(cardValue('Completed positions')).toBe('0')
    expect(cardValue('Open positions')).toBe('0')
    expect(cardValue('Partially closed')).toBe('0')
    expect(screen.getByTestId('today-closed-empty')).toHaveTextContent(
      'No trades closed today yet'
    )
    expect(screen.queryByTestId('today-pending')).not.toBeInTheDocument()
    expect(screen.queryByTestId('today-charges-incomplete')).not.toBeInTheDocument()
  })

  it('shows an open, a partially closed and a closed position with their figures', () => {
    render(
      <TodayPerformance
        today={emptyToday({
          realized_gross: 650,
          charges: 20,
          net: 630,
          realized_net_of_charges: 630,
          exit_legs: 2,
          completed_positions: 1,
          open_positions: 2,
          partially_closed_positions: 1,
          quotes_requested: true,
          unrealized: -250,
          price_statuses: { live: 2 },
          metrics: { trades_count: 1, winning_trades: 1, losing_trades: 0 },
          closed_positions: [todayClosed()],
        })}
        formatCurrency={money}
      />
    )
    expect(cardValue('Realised (gross)')).toBe('Rs 650.00')
    expect(screen.getByText('2 exit fills (incl. partial exits)')).toBeInTheDocument()
    expect(cardValue('Net after est. charges')).toBe('Rs 630.00')
    expect(screen.getByText('after Rs 20.00 charges')).toBeInTheDocument()
    expect(cardValue('Unrealised')).toBe('Rs -250.00')
    expect(screen.getByTestId('today-price-badge')).toHaveTextContent('Live')
    expect(cardValue('Completed positions')).toBe('1')
    expect(screen.getByText('1 wins / 0 losses')).toBeInTheDocument()
    expect(cardValue('Open positions')).toBe('2')
    expect(cardValue('Partially closed')).toBe('1')
    const row = screen.getByTestId('today-closed-row')
    const cells = within(row)
      .getAllByRole('cell')
      .map((c) => c.textContent)
    expect(cells).toEqual([
      'SBINNSE MIS',
      'Long',
      '45',
      '100.00',
      '110.00',
      '09:30',
      '10:15',
      'Rs 450.00',
      'Rs 12.50',
      'Rs 437.50',
      'Closed',
      'A1',
      'flattrade',
    ])
  })

  it('marks net as incomplete and the row charges unavailable when charges are missing', () => {
    render(
      <TodayPerformance
        today={emptyToday({
          realized_gross: 100,
          net: 100,
          charges_complete: false,
          charges_unavailable_legs: 1,
          completed_positions: 1,
          closed_positions: [
            todayClosed({ charges: null, charges_status: 'unavailable', net_pnl: null }),
          ],
        })}
        formatCurrency={money}
      />
    )
    expect(screen.getByTestId('today-charges-incomplete')).toHaveTextContent('incomplete')
    expect(screen.getByTestId('today-charges-incomplete')).toHaveAttribute(
      'title',
      '1 exit fills have no charge estimate: counted as 0'
    )
    const cells = within(screen.getByTestId('today-closed-row')).getAllByRole('cell')
    expect(cells[8]).toHaveTextContent('unavailable')
    expect(cells[9]).toHaveTextContent('—')
  })

  it('says unrealised is not requested by default', () => {
    render(
      <TodayPerformance
        today={emptyToday({ open_positions: 1, price_statuses: { not_requested: 1 } })}
        formatCurrency={money}
      />
    )
    expect(cardValue('Unrealised')).toBe('—')
    expect(screen.getByTestId('today-price-badge')).toHaveTextContent('Not requested')
    expect(screen.getByText('turn on Fetch live prices')).toBeInTheDocument()
  })

  it('explains fills held by the grace window with the earliest attribution time', () => {
    render(
      <TodayPerformance
        today={emptyToday({
          open_positions: 1,
          pending_attribution: {
            ...emptyToday().pending_attribution,
            grace_count: 2,
            earliest_attributable_at: '2026-11-17 11:00:00',
            grace_fills: [
              grace({ id: 8, attributable_from: '2026-11-17 11:00:00', due: true }),
              grace(),
            ],
            other_today: 1,
            other_total: 3,
          },
        })}
        formatCurrency={money}
      />
    )
    const note = screen.getByTestId('today-pending')
    expect(note).toHaveTextContent('2 execution(s) awaiting attribution')
    expect(note).toHaveTextContent('30-minute safety window')
    expect(note).toHaveTextContent('not counted in P&L yet')
    expect(note).toHaveTextContent('Earliest attribution at 11:20 IST')
    expect(note).toHaveTextContent('1 passed the window and wait only for the next ledger sync')
    expect(note).toHaveTextContent('Some belong to an open position')
    expect(note).toHaveTextContent('1 execution(s) of today stay unresolved for another reason')
    // no figure moves: realised stays zero
    expect(cardValue('Realised (gross)')).toBe('Rs 0.00')
  })

  it('shows the entry date of a carried position and explains the gross figure', () => {
    render(
      <TodayPerformance
        today={emptyToday({
          realized_gross: 60,
          net: 60,
          completed_positions: 1,
          closed_positions: [
            todayClosed({
              product: 'CNC',
              opened_at: '2026-11-16 10:00:00',
              opened_session: '2026-11-16',
              gross_pnl: 76,
              realized_today: 60,
              legs: 2,
            }),
          ],
        })}
        formatCurrency={money}
      />
    )
    const cells = within(screen.getByTestId('today-closed-row')).getAllByRole('cell')
    expect(cells[2]).toHaveTextContent('45(2 exit fills)')
    expect(cells[5]).toHaveTextContent('2026-11-16 10:00')
    expect(screen.getByText(/exits made on earlier days are in those days/)).toBeInTheDocument()
  })

  it('opens the fills of a closed row through onSelectClosed', async () => {
    const onSelect = vi.fn()
    render(
      <TodayPerformance
        today={emptyToday({ completed_positions: 1, closed_positions: [todayClosed()] })}
        formatCurrency={money}
        onSelectClosed={onSelect}
      />
    )
    await userEvent.click(screen.getByTestId('today-closed-row'))
    expect(onSelect).toHaveBeenCalledWith(11)
  })
})
