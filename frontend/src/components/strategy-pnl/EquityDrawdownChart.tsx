import {
  BaselineSeries,
  ColorType,
  createChart,
  type IChartApi,
  type ISeriesApi,
  LineSeries,
  LineStyle,
  type UTCTimestamp,
} from 'lightweight-charts'
import { useEffect, useRef, useState } from 'react'
import type { EquityPoint } from '@/api/strategyPnl'

export const NET_LINE_COLOR = '#6366f1'

export function EquityDrawdownChart({
  equityCurve,
  equityCurveNet,
  isDarkMode,
  formatCurrency,
}: {
  equityCurve: EquityPoint[]
  equityCurveNet: EquityPoint[]
  isDarkMode: boolean
  formatCurrency: (v: number) => string
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const equitySeriesRef = useRef<ISeriesApi<'Baseline'> | null>(null)
  const drawdownSeriesRef = useRef<ISeriesApi<'Baseline'> | null>(null)
  const netSeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  // Bumped each time the chart is rebuilt (theme or currency change), so the
  // data effect below reloads the new, empty series.
  const [chartVersion, setChartVersion] = useState(0)

  useEffect(() => {
    if (!containerRef.current) return
    const container = containerRef.current
    if (chartRef.current) {
      chartRef.current.remove()
      chartRef.current = null
    }

    const chart = createChart(container, {
      width: container.offsetWidth,
      height: 400,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: isDarkMode ? '#a6adbb' : '#333',
        panes: {
          enableResize: true,
          separatorColor: isDarkMode ? 'rgba(166, 173, 187, 0.2)' : 'rgba(0, 0, 0, 0.2)',
        },
      },
      grid: {
        vertLines: { visible: false },
        horzLines: {
          color: isDarkMode ? 'rgba(166, 173, 187, 0.1)' : 'rgba(0, 0, 0, 0.08)',
        },
      },
      rightPriceScale: {
        borderColor: isDarkMode ? 'rgba(166, 173, 187, 0.2)' : 'rgba(0, 0, 0, 0.2)',
        scaleMargins: { top: 0.1, bottom: 0.1 },
      },
      timeScale: {
        borderColor: isDarkMode ? 'rgba(166, 173, 187, 0.2)' : 'rgba(0, 0, 0, 0.2)',
        timeVisible: false,
      },
    })

    const equitySeries = chart.addSeries(
      BaselineSeries,
      {
        baseValue: { type: 'price', price: 0 },
        topLineColor: '#22c55e',
        topFillColor1: 'rgba(34, 197, 94, 0.28)',
        topFillColor2: 'rgba(34, 197, 94, 0.02)',
        bottomLineColor: '#ef4444',
        bottomFillColor1: 'rgba(239, 68, 68, 0.02)',
        bottomFillColor2: 'rgba(239, 68, 68, 0.28)',
        lineWidth: 2,
        priceScaleId: 'right',
        priceFormat: { type: 'custom', formatter: (p: number) => formatCurrency(p) },
      },
      0
    )

    const netSeries = chart.addSeries(
      LineSeries,
      {
        color: NET_LINE_COLOR,
        lineWidth: 2,
        lineStyle: LineStyle.Dashed,
        priceScaleId: 'right',
        lastValueVisible: false,
        priceLineVisible: false,
        priceFormat: { type: 'custom', formatter: (p: number) => formatCurrency(p) },
      },
      0
    )

    const drawdownSeries = chart.addSeries(
      BaselineSeries,
      {
        baseValue: { type: 'price', price: 0 },
        topLineColor: '#eab308',
        topFillColor1: 'rgba(234, 179, 8, 0)',
        topFillColor2: 'rgba(234, 179, 8, 0)',
        bottomLineColor: '#eab308',
        bottomFillColor1: 'rgba(234, 179, 8, 0.04)',
        bottomFillColor2: 'rgba(234, 179, 8, 0.30)',
        lineWidth: 2,
        priceScaleId: 'right',
        priceFormat: { type: 'custom', formatter: (p: number) => formatCurrency(p) },
      },
      1
    )

    const panes = chart.panes()
    if (panes.length > 1) {
      panes[0].setHeight(300)
      panes[1].setHeight(100)
    }

    chartRef.current = chart
    equitySeriesRef.current = equitySeries
    drawdownSeriesRef.current = drawdownSeries
    netSeriesRef.current = netSeries
    setChartVersion((v) => v + 1)

    const handleResize = () => {
      chart.applyOptions({ width: container.offsetWidth })
    }
    window.addEventListener('resize', handleResize)

    return () => {
      window.removeEventListener('resize', handleResize)
      chart.remove()
      chartRef.current = null
    }
  }, [isDarkMode, formatCurrency])

  useEffect(() => {
    if (chartVersion === 0 || !equitySeriesRef.current || !drawdownSeriesRef.current) return
    const toTime = (d: string) =>
      Math.floor(new Date(`${d}T00:00:00Z`).getTime() / 1000) as UTCTimestamp

    const equityData = equityCurve.map((p) => ({ time: toTime(p.date), value: p.value }))
    let runningMax = Number.NEGATIVE_INFINITY
    const drawdownData = equityCurve.map((p) => {
      runningMax = Math.max(runningMax, p.value)
      return { time: toTime(p.date), value: p.value - runningMax }
    })

    if (equityData.length > 0) {
      equitySeriesRef.current.setData(equityData)
      drawdownSeriesRef.current.setData(drawdownData)
      netSeriesRef.current?.setData(
        equityCurveNet.map((p) => ({ time: toTime(p.date), value: p.value }))
      )
      chartRef.current?.timeScale().fitContent()
    }
  }, [equityCurve, equityCurveNet, chartVersion])

  return <div ref={containerRef} className="relative" style={{ height: '400px' }} />
}
