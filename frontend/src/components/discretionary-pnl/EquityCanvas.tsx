import {
  BaselineSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  LineStyle,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts'
import { useEffect, useMemo, useRef, useState } from 'react'
import { type CurvePoint, fmtClock } from '@/lib/discretionaryPnlView'

const EQUITY_HEIGHT = 320
const DRAWDOWN_HEIGHT = 110
/** Markers are drawn for every point while the curve is short enough to read them. */
const MARKER_LIMIT = 60
export const DRAWDOWN_COLOR = '#d97706'

/** x label of a point: its position number, with the close time while there is room. */
export function axisLabel(p: CurvePoint, mode: 'position' | 'day', total: number): string {
  if (mode === 'day') return p.closed_at
  return total <= 10 ? `#${p.index} ${fmtClock(p.closed_at)}` : `#${p.index}`
}

/**
 * Cumulative net P&L (upper pane) and drawdown below the peak (lower pane). The x axis is the
 * closed-position number in close order, not a clock: several positions can close in the same
 * second, and no timestamp is invented. Hovering shows the position, its close time, symbol,
 * net P&L, the cumulative figure and the drawdown.
 */
export function EquityCanvas({
  points,
  mode,
  isDarkMode,
  formatCurrency,
}: {
  points: CurvePoint[]
  mode: 'position' | 'day'
  isDarkMode: boolean
  formatCurrency: (v: number) => string
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const byTime = useMemo(() => new Map(points.map((p) => [p.index, p])), [points])
  const [hover, setHover] = useState<{ point: CurvePoint; x: number; y: number } | null>(null)

  useEffect(() => {
    const container = containerRef.current
    if (!container || points.length === 0) return
    const grid = isDarkMode ? 'rgba(166, 173, 187, 0.1)' : 'rgba(0, 0, 0, 0.08)'
    const border = isDarkMode ? 'rgba(166, 173, 187, 0.2)' : 'rgba(0, 0, 0, 0.2)'
    const total = points.length
    const chart = createChart(container, {
      width: container.offsetWidth,
      height: EQUITY_HEIGHT + DRAWDOWN_HEIGHT + 30,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: isDarkMode ? '#cbd1dc' : '#333',
        panes: { enableResize: false, separatorColor: border },
      },
      grid: { vertLines: { visible: false }, horzLines: { color: grid } },
      rightPriceScale: { borderColor: border, scaleMargins: { top: 0.1, bottom: 0.1 } },
      timeScale: {
        borderColor: border,
        timeVisible: false,
        rightOffset: 1,
        tickMarkFormatter: (time: Time) => {
          const p = byTime.get(Number(time))
          return p ? axisLabel(p, mode, total) : ''
        },
      },
      localization: {
        timeFormatter: (time: Time) => {
          const p = byTime.get(Number(time))
          return p ? axisLabel(p, mode, total) : ''
        },
      },
    })
    // The zero line must always be inside the view, and a single point or a flat drawdown must
    // not zoom the scale into cents.
    const maxDd = Math.max(...points.map((p) => p.drawdown), 0)
    const withZero = (
      orig: () => { priceRange: { minValue: number; maxValue: number } } | null
    ) => {
      const r = orig()
      let lo = Math.min(r?.priceRange.minValue ?? 0, 0)
      let hi = Math.max(r?.priceRange.maxValue ?? 0, 0)
      if (hi - lo < 1) {
        lo -= 0.5
        hi += 0.5
      }
      return { priceRange: { minValue: lo, maxValue: hi } }
    }
    const money = { type: 'custom' as const, formatter: (v: number) => formatCurrency(v) }
    const equity = chart.addSeries(
      BaselineSeries,
      {
        baseValue: { type: 'price', price: 0 },
        topLineColor: '#16a34a',
        topFillColor1: 'rgba(22, 163, 74, 0.28)',
        topFillColor2: 'rgba(22, 163, 74, 0.02)',
        bottomLineColor: '#dc2626',
        bottomFillColor1: 'rgba(220, 38, 38, 0.02)',
        bottomFillColor2: 'rgba(220, 38, 38, 0.28)',
        lineWidth: 2,
        title: 'Net P&L (Rs)',
        priceFormat: money,
        autoscaleInfoProvider: withZero,
      },
      0
    )
    const drawdown = chart.addSeries(
      BaselineSeries,
      {
        baseValue: { type: 'price', price: 0 },
        topLineColor: DRAWDOWN_COLOR,
        topFillColor1: 'rgba(217, 119, 6, 0)',
        topFillColor2: 'rgba(217, 119, 6, 0)',
        bottomLineColor: DRAWDOWN_COLOR,
        bottomFillColor1: 'rgba(217, 119, 6, 0.05)',
        bottomFillColor2: 'rgba(217, 119, 6, 0.35)',
        lineWidth: 2,
        title: 'Drawdown (Rs)',
        priceFormat: money,
        autoscaleInfoProvider: () => ({
          priceRange: {
            minValue: -Math.max(maxDd * 1.1, 1),
            maxValue: Math.max(maxDd * 0.05, 0.1),
          },
        }),
      },
      1
    )
    equity.setData(points.map((p) => ({ time: p.index as UTCTimestamp, value: p.equity })))
    drawdown.setData(
      points.map((p) => ({
        time: p.index as UTCTimestamp,
        value: p.drawdown === 0 ? 0 : -p.drawdown,
      }))
    )
    // The zero line is a reference, not a data point.
    equity.createPriceLine({
      price: 0,
      color: isDarkMode ? '#a6adbb' : '#6b7280',
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      axisLabelVisible: false,
      title: '0',
    })
    if (total <= MARKER_LIMIT) {
      const markers: SeriesMarker<Time>[] = points.map((p) => ({
        time: p.index as UTCTimestamp,
        position: 'inBar',
        shape: 'circle',
        color: p.net > 0 ? '#16a34a' : p.net < 0 ? '#dc2626' : '#6b7280',
        size: 1,
      }))
      createSeriesMarkers(equity, markers)
    }
    const panes = chart.panes()
    if (panes.length > 1) {
      panes[0].setHeight(EQUITY_HEIGHT)
      panes[1].setHeight(DRAWDOWN_HEIGHT)
    }
    chart.timeScale().fitContent()
    chart.subscribeCrosshairMove((param) => {
      const p = param.time == null ? undefined : byTime.get(Number(param.time))
      if (!p || !param.point) {
        setHover(null)
        return
      }
      setHover({ point: p, x: param.point.x, y: param.point.y })
    })
    chartRef.current = chart
    const onResize = () => chart.applyOptions({ width: container.offsetWidth })
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      chart.remove()
      chartRef.current = null
    }
  }, [points, mode, isDarkMode, formatCurrency, byTime])

  const width = containerRef.current?.offsetWidth ?? 0
  const tipLeft = hover ? (width > 0 && hover.x > width - 230 ? hover.x - 220 : hover.x + 14) : 0
  return (
    <div className="relative" data-testid="equity-canvas">
      <div ref={containerRef} style={{ height: `${EQUITY_HEIGHT + DRAWDOWN_HEIGHT + 30}px` }} />
      {hover && (
        <div
          className="pointer-events-none absolute z-10 w-[210px] rounded-md border bg-popover p-2 text-xs text-popover-foreground shadow-md"
          style={{ left: Math.max(tipLeft, 4), top: Math.max(hover.y - 40, 4) }}
          data-testid="equity-tooltip"
        >
          <div className="font-medium">
            {mode === 'position' ? `Position ${hover.point.index}` : hover.point.closed_at}
          </div>
          {mode === 'position' && (
            <>
              <div>Closed {hover.point.closed_at.replace('T', ' ').slice(0, 16)}</div>
              <div>
                {hover.point.symbol} {hover.point.direction === 'LONG' ? 'Long' : 'Short'}
              </div>
            </>
          )}
          <div>
            {mode === 'position' ? 'Net P&L' : 'Day net P&L'}: {formatCurrency(hover.point.net)}
          </div>
          <div>Cumulative net P&L: {formatCurrency(hover.point.equity)}</div>
          <div>Drawdown: {formatCurrency(hover.point.drawdown)}</div>
        </div>
      )}
    </div>
  )
}
