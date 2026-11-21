import { act, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { netEquityPerTrade } from '@/lib/discretionaryPnlView'

const lw = vi.hoisted(() => ({
  series: [] as { setData: ReturnType<typeof vi.fn>; opts: unknown; pane: number }[],
  markers: [] as unknown[][],
  priceLines: [] as unknown[],
  crosshair: null as null | ((p: unknown) => void),
  removed: 0,
  chartOpts: null as unknown,
}))

vi.mock('lightweight-charts', () => ({
  BaselineSeries: 'Baseline',
  ColorType: { Solid: 'solid' },
  LineStyle: { Dashed: 2 },
  createSeriesMarkers: (_s: unknown, m: unknown[]) => {
    lw.markers.push(m)
  },
  createChart: (_el: unknown, opts: unknown) => {
    lw.chartOpts = opts
    return {
      addSeries: (_t: unknown, o: unknown, pane: number) => {
        const s = {
          setData: vi.fn(),
          opts: o,
          pane,
          createPriceLine: (l: unknown) => lw.priceLines.push(l),
        }
        lw.series.push(s)
        return s
      },
      panes: () => [{ setHeight: vi.fn() }, { setHeight: vi.fn() }],
      timeScale: () => ({ fitContent: vi.fn() }),
      subscribeCrosshairMove: (cb: (p: unknown) => void) => {
        lw.crosshair = cb
      },
      applyOptions: vi.fn(),
      remove: () => {
        lw.removed += 1
      },
    }
  },
}))

const { DiscretionaryEquityChart } = await import('./DiscretionaryEquityChart')
const { drawdownReconciliation } = await import('@/lib/discretionaryPnlView')

const money = (v: number) => `Rs ${v.toFixed(2)}`

function trades(nets: (number | null)[]) {
  return nets.map((n, i) => ({
    id: i + 1,
    closed_at: `2026-11-16 10:0${i}:00`,
    symbol: i % 2 === 0 ? 'RELIANCE' : 'INFY',
    direction: (i % 2 === 0 ? 'LONG' : 'SHORT') as 'LONG' | 'SHORT',
    net_pnl: n,
  }))
}

function renderChart(nets: (number | null)[]) {
  const curve = netEquityPerTrade(trades(nets))
  const reconciliation = drawdownReconciliation({
    curve,
    mode: 'position',
    tradesShown: nets.length,
    tradesTotal: nets.length,
    risk: undefined,
    formatCurrency: money,
  })
  return {
    curve,
    ...render(
      <DiscretionaryEquityChart
        curve={curve}
        mode="position"
        reconciliation={reconciliation}
        basis="Net realised P&L per closed trade."
        isDarkMode={false}
        formatCurrency={money}
      />
    ),
  }
}

beforeEach(() => {
  lw.series.length = 0
  lw.markers.length = 0
  lw.priceLines.length = 0
  lw.crosshair = null
  lw.removed = 0
})

describe('discretionary equity chart', () => {
  for (const n of [1, 2, 3]) {
    it(`draws ${n} closed position(s) as real points with a marker each, never an empty chart`, () => {
      const nets = [40, -15, 25].slice(0, n)
      renderChart(nets)
      expect(lw.series).toHaveLength(2)
      const [equity, drawdown] = lw.series
      expect(equity.pane).toBe(0)
      expect(drawdown.pane).toBe(1)
      const eqData = equity.setData.mock.calls[0][0] as { time: number; value: number }[]
      expect(eqData).toHaveLength(n)
      // x is the position number, nothing is interpolated or invented
      expect(eqData.map((d) => d.time)).toEqual([1, 2, 3].slice(0, n))
      expect(lw.markers[0]).toHaveLength(n)
      expect(screen.getByTestId('equity-few-note')).toHaveTextContent(`Only ${n} point`)
      // the zero line is a price line, not a point
      expect(lw.priceLines).toHaveLength(1)
      expect((lw.priceLines[0] as { price: number }).price).toBe(0)
    })
  }

  it('plots the drawdown below zero in its own pane and labels both series', () => {
    renderChart([-50, 30, -20, 80])
    const dd = lw.series[1].setData.mock.calls[0][0] as { value: number }[]
    expect(dd.map((d) => d.value)).toEqual([-50, -20, -40, 0])
    expect((lw.series[0].opts as { title: string }).title).toContain('Net P&L')
    expect((lw.series[1].opts as { title: string }).title).toContain('Drawdown')
    expect(screen.getByText(/Drawdown below the peak, lower pane/)).toBeInTheDocument()
  })

  it('shows position, close time, symbol, net, cumulative and drawdown on hover', () => {
    renderChart([-50, 30])
    act(() => lw.crosshair?.({ time: 2, point: { x: 120, y: 80 } }))
    const tip = screen.getByTestId('equity-tooltip')
    expect(tip).toHaveTextContent('Position 2')
    expect(tip).toHaveTextContent('Closed 2026-11-16 10:01')
    expect(tip).toHaveTextContent('INFY Short')
    expect(tip).toHaveTextContent('Net P&L: Rs 30.00')
    expect(tip).toHaveTextContent('Cumulative net P&L: Rs -20.00')
    expect(tip).toHaveTextContent('Drawdown: Rs 20.00')
    act(() => lw.crosshair?.({ time: undefined }))
    expect(screen.queryByTestId('equity-tooltip')).not.toBeInTheDocument()
  })

  it('has a keyboard route to the values: focusable summary and a data table', () => {
    renderChart([-50, 30, -20, 80])
    const text = screen.getByTestId('equity-text-summary')
    expect(text).toHaveAttribute('tabindex', '0')
    expect(text).toHaveTextContent('4 closed trades')
    expect(text).toHaveTextContent(
      'first Rs -50.00, last Rs 40.00, lowest Rs -50.00, highest Rs 40.00'
    )
    expect(text).toHaveTextContent('Maximum drawdown Rs 50.00')
    const table = screen.getByTestId('equity-data-table')
    expect(table.tagName).toBe('DETAILS')
    expect(table.querySelectorAll('tbody tr')).toHaveLength(4)
    expect(screen.getByRole('img').getAttribute('aria-label')).toContain(
      'Equity per closed trade'
    )
  })

  it('states the basis and the reconciliation sentence under the chart', () => {
    renderChart([10, -5])
    expect(screen.getByTestId('net-equity-caption')).toHaveTextContent('Net realised P&L')
    expect(screen.getByTestId('equity-reconciliation')).toHaveTextContent('no Risk card')
  })

  it('removes the chart on unmount', () => {
    const { unmount } = renderChart([10])
    unmount()
    expect(lw.removed).toBeGreaterThan(0)
  })
})
