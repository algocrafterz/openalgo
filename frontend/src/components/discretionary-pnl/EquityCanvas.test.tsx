import { act, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { netEquityFromDailyRows, netEquityPerTrade } from '@/lib/discretionaryPnlView'

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
      // the zero line is a price line, not a point
      expect(lw.priceLines).toHaveLength(1)
      expect((lw.priceLines[0] as { price: number }).price).toBe(0)
    })
  }

  it('plots the drawdown below zero in its own pane and labels both series', () => {
    renderChart([-50, 30, -20, 80])
    const dd = lw.series[1].setData.mock.calls[0][0] as { value: number }[]
    expect(dd.map((d) => d.value)).toEqual([-50, -20, -40, 0])
    expect((lw.series[0].opts as { title: string }).title).toContain('Cumulative net P&L')
    expect((lw.series[1].opts as { title: string }).title).toContain('Drawdown')
    expect(screen.getByText(/Drawdown: fall below the highest point so far/)).toBeInTheDocument()
    expect(screen.getByText(/green a winning trade, red a losing trade/)).toBeInTheDocument()
  })

  it('shows position, close time, symbol, net, cumulative and drawdown on hover', () => {
    renderChart([-50, 30])
    act(() => lw.crosshair?.({ time: 2, point: { x: 120, y: 80 } }))
    const tip = screen.getByTestId('equity-tooltip')
    expect(tip).toHaveTextContent('Trade 2')
    expect(tip).toHaveTextContent('Closed 2026-11-16 10:01')
    expect(tip).toHaveTextContent('INFY Short')
    expect(tip).toHaveTextContent('Trade net P&L: Rs 30.00')
    expect(tip).toHaveTextContent('Cumulative net P&L: Rs -20.00')
    expect(tip).toHaveTextContent('Drawdown: Rs 20.00')
    act(() => lw.crosshair?.({ time: undefined }))
    expect(screen.queryByTestId('equity-tooltip')).not.toBeInTheDocument()
  })

  it('has a keyboard route to the values and no table repeating Closed trades', () => {
    renderChart([-50, 30, -20, 80])
    const text = screen.getByTestId('equity-text-summary')
    expect(text).toHaveAttribute('tabindex', '0')
    expect(text).toHaveTextContent('4 closed trades')
    expect(text).toHaveTextContent(
      'first Rs -50.00, last Rs 40.00, lowest Rs -50.00, highest Rs 40.00'
    )
    expect(text).toHaveTextContent('Maximum drawdown Rs 50.00')
    expect(screen.queryByTestId('equity-data-table')).not.toBeInTheDocument()
    expect(screen.getByRole('img').getAttribute('aria-label')).toContain('Trade-by-trade P&L')
  })

  it('states the basis and adds no comparison note when there is no Risk figure', () => {
    renderChart([10, -5])
    expect(screen.getByTestId('net-equity-caption')).toHaveTextContent('Net realised P&L')
    expect(screen.queryByTestId('equity-reconciliation')).not.toBeInTheDocument()
  })

  it('removes the chart on unmount', () => {
    const { unmount } = renderChart([10])
    unmount()
    expect(lw.removed).toBeGreaterThan(0)
  })
})

describe('end-of-day curve on a calendar axis', () => {
  function dayRow(period: string, net: number, extra: Record<string, unknown> = {}) {
    return {
      period,
      label: period,
      start: period,
      end: period,
      trades: 1,
      wins: 1,
      losses: 0,
      realized_gross: net,
      charges: 0,
      charges_unavailable_trades: 0,
      net_of_charges: net,
      net_charges_complete: true,
      ...extra,
    }
  }

  // Fri 13 and Tue 17 have a row; Mon 16 does not; Sat 14 and Sun 15 are the weekend.
  function renderDays(uncollected: string[] = []) {
    const curve = netEquityFromDailyRows(
      [dayRow('2026-11-13', 50), dayRow('2026-11-17', -20)],
      uncollected
    )
    return render(
      <DiscretionaryEquityChart
        curve={curve}
        mode="day"
        reconciliation={{ kind: 'day_curve', text: '' }}
        basis="Net per day."
        notCollectedDays={uncollected}
        uncollectedNote={
          uncollected.length ? 'Your broker trades were not collected on 16 Nov.' : null
        }
        isDarkMode={false}
        formatCurrency={money}
      />
    )
  }

  it('leaves a gap (a point without a value) for a weekday without a row, never a zero', () => {
    renderDays()
    const eq = lw.series[0].setData.mock.calls[0][0] as { time: number; value?: number }[]
    // Fri 13, Mon 16 (gap), Tue 17: the weekend is not a slot
    expect(eq).toHaveLength(3)
    expect(eq.map((d) => d.value)).toEqual([50, undefined, 30])
    expect(eq.some((d) => d.value === 0)).toBe(false)
    const dd = lw.series[1].setData.mock.calls[0][0] as { value?: number }[]
    expect(dd.map((d) => d.value)).toEqual([0, undefined, -20])
    // x is the real date, so the gap is a day of the calendar
    const times = eq.map((d) => d.time)
    expect(times[1] - times[0]).toBe(3 * 86400)
    expect(times[2] - times[1]).toBe(86400)
  })

  it('draws a dot on every real day, amber for one that is incomplete, and none on a gap', () => {
    renderDays(['2026-11-13'])
    const markers = lw.markers[0] as { color: string }[]
    expect(markers).toHaveLength(2)
    expect(markers[0].color).toBe('#d97706')
    expect(markers[1].color).toBe('#dc2626')
  })

  it('says a weekday without a dot has no value, and flags days that were not collected', () => {
    renderDays(['2026-11-16'])
    expect(screen.getByTestId('equity-axis-note')).toHaveTextContent(
      'A weekday with no dot had no recorded trades, so it has no value and the line does not join across it'
    )
    expect(screen.getByTestId('equity-uncollected-days')).toHaveTextContent(
      'Your broker trades were not collected on 16 Nov.'
    )
  })

  it('shows the date, not an index, in the tooltip', () => {
    renderDays()
    const t = (lw.series[0].setData.mock.calls[0][0] as { time: number }[])[2].time
    act(() => lw.crosshair?.({ time: t, point: { x: 100, y: 60 } }))
    const tip = screen.getByTestId('equity-tooltip')
    expect(tip).toHaveTextContent('2026-11-17')
    expect(tip).toHaveTextContent('Day net P&L: Rs -20.00')
    // a gap day has no point, so no tooltip
    const gap = (lw.series[0].setData.mock.calls[0][0] as { time: number }[])[1].time
    act(() => lw.crosshair?.({ time: gap, point: { x: 100, y: 60 } }))
    expect(screen.queryByTestId('equity-tooltip')).not.toBeInTheDocument()
  })
})
