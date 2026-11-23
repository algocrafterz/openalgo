import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { monthTitle } from '@/lib/discretionaryCalendar'
import { summary } from '@/test/discretionaryFixtures'
import { render, screen, userEvent, waitFor } from '@/test/test-utils'

const summaryFn = vi.fn()

vi.mock('@/api/discretionaryPnl', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/discretionaryPnl')>()
  return {
    ...actual,
    discretionaryPnlApi: { summary: (...a: unknown[]) => summaryFn(...a) },
  }
})

const { PnlCalendar } = await import('./PnlCalendar')

const TODAY = '2026-11-23'

function calendar(anchorDate: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={client}>
      <PnlCalendar
        anchorDate={anchorDate}
        selectedRange={undefined}
        today={TODAY}
        filters={{}}
        category="all"
        reliableSince="2026-01-01"
        formatCurrency={(v) => `Rs ${v.toFixed(2)}`}
        onSelectDate={() => {}}
      />
    </QueryClientProvider>
  )
}

const shownMonth = () => screen.getByTestId('calendar-month').textContent

beforeEach(() => {
  vi.clearAllMocks()
  summaryFn.mockResolvedValue(summary())
})

describe('P&L calendar month follows the report date', () => {
  it('starts on the month of the report date', () => {
    render(calendar('2026-09-05'))
    expect(shownMonth()).toBe(monthTitle('2026-09'))
  })

  it('keeps a month the trader browsed to while the report date does not change', async () => {
    const { rerender } = render(calendar('2026-11-10'))
    expect(shownMonth()).toBe(monthTitle('2026-11'))
    await userEvent.click(screen.getByRole('button', { name: 'Previous month' }))
    await waitFor(() => expect(shownMonth()).toBe(monthTitle('2026-10')))
    rerender(calendar('2026-11-10'))
    expect(shownMonth()).toBe(monthTitle('2026-10'))
  })

  it('moves to the month of a new report date, even after browsing', async () => {
    const { rerender } = render(calendar('2026-11-10'))
    await userEvent.click(screen.getByRole('button', { name: 'Previous month' }))
    await waitFor(() => expect(shownMonth()).toBe(monthTitle('2026-10')))
    rerender(calendar('2026-08-12'))
    await waitFor(() => expect(shownMonth()).toBe(monthTitle('2026-08')))
  })

  it('never follows a report date past the current month', () => {
    const { rerender } = render(calendar('2026-11-10'))
    rerender(calendar('2027-02-01'))
    expect(shownMonth()).toBe(monthTitle('2026-11'))
  })
})
