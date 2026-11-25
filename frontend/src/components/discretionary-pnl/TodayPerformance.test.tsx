import { describe, expect, it, vi } from 'vitest'
import { emptyToday, todayClosed } from '@/test/discretionaryFixtures'
import { render, screen, userEvent } from '@/test/test-utils'
import { TodayLine } from './TodayPerformance'

const money = (v: number) => `Rs ${v.toFixed(2)}`

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

describe('TodayLine', () => {
  it('is one line, with the empty text when nothing was closed today', () => {
    render(<TodayLine today={emptyToday()} formatCurrency={money} />)
    const line = screen.getByTestId('today-line')
    expect(screen.getByTestId('today-summary')).toHaveTextContent(
      `Today (${emptyToday().session_date}): no closed trades, 0 open.`
    )
    // no panel, no tiles and no second closed-trades table
    expect(screen.queryByText("Today's Performance")).not.toBeInTheDocument()
    expect(screen.queryByTestId('today-closed-table')).not.toBeInTheDocument()
    expect(line.querySelectorAll('table')).toHaveLength(0)
    // no net figure is invented for a day with no closed trade
    expect(screen.queryByTestId('today-net')).not.toBeInTheDocument()
    expect(screen.queryByTestId('today-pending')).not.toBeInTheDocument()
    expect(screen.queryByTestId('today-charges-incomplete')).not.toBeInTheDocument()
  })

  it('shows net, closed and open in the line, and demotes the other counts to hint text', () => {
    render(
      <TodayLine
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
    expect(screen.getByTestId('today-summary')).toHaveTextContent(
      'net Rs 630.00 after estimated charges, 1 closed, 2 open.'
    )
    const hint = screen.getByTestId('today-hint')
    expect(hint).toHaveTextContent('gross Rs 650.00')
    expect(hint).toHaveTextContent('2 exit fills (incl. partial exits)')
    expect(hint).toHaveTextContent('1 partially closed')
    expect(hint).toHaveTextContent('1 wins / 0 losses')
    expect(hint).toHaveTextContent('charges Rs 20.00')
    expect(hint).toHaveTextContent('unrealised Rs -250.00')
    expect(screen.getByTestId('today-price-badge')).toHaveTextContent('Live')
  })

  it('marks net as incomplete with the unchanged "counted as 0" disclosure', () => {
    render(
      <TodayLine
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
  })

  it('says unrealised is not requested by default, without a number', () => {
    render(
      <TodayLine
        today={emptyToday({ open_positions: 1, price_statuses: { not_requested: 1 } })}
        formatCurrency={money}
      />
    )
    expect(screen.getByTestId('today-hint')).toHaveTextContent(
      'unrealised: turn on Fetch live prices'
    )
    expect(screen.queryByTestId('today-price-badge')).not.toBeInTheDocument()
  })

  it('explains fills held by the grace window with the earliest attribution time', () => {
    render(
      <TodayLine
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
    expect(note).toHaveTextContent('2 execution(s) await attribution')
    expect(note).toHaveTextContent('30-minute window')
    expect(note).toHaveTextContent('not in P&L yet')
    expect(note).toHaveTextContent('Earliest: 11:20 IST')
    expect(note).toHaveTextContent('1 wait only for the next ledger sync')
    expect(note).toHaveTextContent('An open position may still change')
    expect(note).toHaveTextContent('1 execution(s) today are unresolved and not in P&L')
    // no figure moves: nothing was closed, so no net is shown
    expect(screen.queryByTestId('today-net')).not.toBeInTheDocument()
  })

  it('has no link to a provisional panel, whether or not provisional data exists', () => {
    const today = emptyToday({
      pending_attribution: {
        ...emptyToday().pending_attribution,
        grace_count: 1,
        grace_fills: [grace()],
      },
    })
    const { rerender } = render(<TodayLine today={today} formatCurrency={money} />)
    expect(screen.getByTestId('today-pending')).toBeInTheDocument()
    expect(screen.queryByTestId('today-provisional-link')).not.toBeInTheDocument()
    rerender(<TodayLine today={today} formatCurrency={money} provisionalShown />)
    expect(screen.getByTestId('today-pending')).toBeInTheDocument()
    expect(screen.queryByTestId('today-provisional-link')).not.toBeInTheDocument()
    expect(screen.queryByText('See provisional figures')).not.toBeInTheDocument()
  })

  it("offers to show today's trades only when the page can make today the report date", async () => {
    const onShow = vi.fn()
    const { rerender } = render(<TodayLine today={emptyToday()} formatCurrency={money} />)
    expect(screen.queryByRole('button', { name: /show today/i })).not.toBeInTheDocument()
    rerender(<TodayLine today={emptyToday()} formatCurrency={money} onShowToday={onShow} />)
    await userEvent.click(screen.getByRole('button', { name: /show today/i }))
    expect(onShow).toHaveBeenCalledTimes(1)
  })
})
