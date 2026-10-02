import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, MemoryRouter, RouterProvider } from 'react-router'
import { describe, expect, it } from 'vitest'
import { makeCard, makeLatest } from '@/test/backtestResultsFixtures'
import { RelatedStrategies } from './RelatedStrategies'

const cls = (c: string) => ({
  strategy_category: c,
  strategy_tags: [],
  trading_style: 'Intraday',
  direction: null,
  timeframe: '5m',
  market: 'NSE Equities',
})
const cur = makeCard({
  id: 'CUR',
  title: 'Current',
  strategy_category: 'Mean Reversion',
  strategy_tags: ['VWAP', 'Pullback'],
})
const sib = makeCard({
  id: 'SIB',
  title: 'RSI Mean Reversion',
  strategy_category: 'Mean Reversion',
  classification: cls('Mean Reversion'),
  latest_run: makeLatest({ run_verdict: 'EDGE' }),
})
const tagged = makeCard({ id: 'TAG', title: 'Tagged one', strategy_tags: ['vwap', 'Pullback'] })

function renderIt() {
  return render(
    <MemoryRouter>
      <RelatedStrategies current={cur} ideas={[cur, sib, tagged]} />
    </MemoryRouter>
  )
}

describe('RelatedStrategies', () => {
  it('shows siblings under the category with classification and verdict', () => {
    renderIt()
    const group = screen.getByTestId('related-category')
    expect(within(group).getByText('Mean Reversion')).toBeInTheDocument()
    expect(within(group).getByText('RSI Mean Reversion')).toBeInTheDocument()
    expect(within(group).getByText(/Intraday/)).toBeInTheDocument()
    expect(within(group).getByText('edge')).toBeInTheDocument()
    expect(within(group).queryByText('Current')).toBeNull()
  })

  it('shows shared tag groups', () => {
    renderIt()
    const group = screen.getByTestId('related-tags')
    expect(within(group).getByText('Shares tags: vwap, Pullback')).toBeInTheDocument()
    expect(within(group).getByText('Tagged one')).toBeInTheDocument()
  })

  it('links to the idea page with exactly one navigation per click', async () => {
    const router = createMemoryRouter(
      [
        {
          path: '/',
          element: <RelatedStrategies current={cur} ideas={[cur, sib, tagged]} />,
        },
        { path: '/backtest-results/:id', element: <div>idea page</div> },
      ],
      { initialEntries: ['/'] }
    )
    const keys = new Set<string>()
    router.subscribe((state) => keys.add(state.location.key))
    render(<RouterProvider router={router} />)
    const link = screen.getByRole('link', { name: 'RSI Mean Reversion' })
    expect(link).toHaveAttribute('href', '/backtest-results/SIB')
    await userEvent.click(link)
    expect(await screen.findByText('idea page')).toBeInTheDocument()
    expect(keys.size).toBe(1)
    expect(router.state.location.pathname).toBe('/backtest-results/SIB')
  })

  it('uses an h2 section heading', () => {
    renderIt()
    expect(
      screen.getByRole('heading', { level: 2, name: 'Related strategies' })
    ).toBeInTheDocument()
  })

  it('renders nothing when there is nothing related', () => {
    const { container } = render(
      <MemoryRouter>
        <RelatedStrategies current={cur} ideas={[cur]} />
      </MemoryRouter>
    )
    expect(container).toBeEmptyDOMElement()
  })
})
