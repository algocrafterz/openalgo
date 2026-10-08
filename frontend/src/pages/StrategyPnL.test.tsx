import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { StrategyLeg, StrategyPnl } from '@/api/strategyPnl'
import StrategyPnL from './StrategyPnL'

const leg = (o: Partial<StrategyLeg>): StrategyLeg => ({
  symbol: 'SBIN',
  exchange: 'NSE',
  product: 'MIS',
  quantity: 0,
  average_price: 0,
  ltp: null,
  realized: 10,
  today_realized: 0,
  unrealized: 0,
  ...o,
})

const strategy: StrategyPnl = {
  strategy: 'ORB',
  realized: 30,
  today_realized: -12.5,
  unrealized: 0,
  total: 30,
  today_total: -12.5,
  open_quantity: 0,
  unpriced_legs: 0,
  legs: [
    leg({ symbol: 'DONETODAY', today_realized: -12.5 }),
    leg({ symbol: 'OLDONE' }),
    leg({ symbol: 'OLDTWO' }),
  ],
}

vi.mock('@/api/strategyPnl', () => ({
  strategyPnlApi: {
    get: vi.fn(async () => ({ status: 'success', mode: 'analyze', strategies: [strategy] })),
  },
}))
vi.mock('@/hooks/useOrderEventRefresh', () => ({ useOrderEventRefresh: () => undefined }))
vi.mock('@/hooks/useLivePrice', () => ({
  useLivePrice: (items: unknown[]) => ({ data: items, isLive: false, isPaused: false }),
}))
vi.mock('@/stores/authStore', () => ({ useAuthStore: () => ({ user: { broker: 'flattrade' } }) }))
vi.mock('@/stores/themeStore', () => ({ onModeChange: () => () => undefined }))
vi.mock('@/components/DataHealthBanner', () => ({ DataHealthBanner: () => null }))

describe('StrategyPnL legs table', () => {
  it('hides flat legs with no P&L today and restores them on toggle', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <StrategyPnL />
      </QueryClientProvider>
    )
    await userEvent.click(await screen.findByRole('button', { name: /ORB/ }))

    expect(screen.getByText('DONETODAY')).toBeTruthy()
    expect(screen.queryByText('OLDONE')).toBeNull()
    expect(screen.getByText(/2 flat leg\(s\) with no P&L today hidden/)).toBeTruthy()

    await userEvent.click(screen.getByRole('button', { name: 'Show flat legs' }))
    const rows = screen.getAllByRole('row')
    expect(within(rows[0]).getByText('Symbol')).toBeTruthy()
    expect(screen.getByText('OLDONE')).toBeTruthy()
    expect(screen.getByText('OLDTWO')).toBeTruthy()
    expect(screen.getByText('Showing all 3 legs')).toBeTruthy()

    await userEvent.click(screen.getByRole('button', { name: 'Hide flat legs' }))
    expect(screen.queryByText('OLDTWO')).toBeNull()
  })
})
