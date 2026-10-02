import { ColorType, createChart, LineSeries, type UTCTimestamp } from 'lightweight-charts'
import { useEffect, useRef } from 'react'
import type { SeriesPoint } from '@/api/backtestResults'
import { toLineData } from '@/lib/backtestResults'
import { useThemeStore } from '@/stores/themeStore'

export interface ChartSeries {
  label: string
  color: string
  points: SeriesPoint[]
}

interface LineChartProps {
  title: string
  series: ChartSeries[]
  height?: number
  /** Zero-or-negative series such as drawdown: explicit sign on the axis and a zero line. */
  signed?: boolean
}

/** Line chart of one or more R-multiple series, one point per trading day. */
export function LineChart({ title, series, height = 260, signed = false }: LineChartProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const isDark = useThemeStore((s) => s.mode) === 'dark'
  const hasData = series.some((s) => s.points.length > 0)

  useEffect(() => {
    const container = containerRef.current
    if (!container || !hasData) return

    const border = isDark ? 'rgba(166, 173, 187, 0.2)' : 'rgba(0, 0, 0, 0.2)'
    const chart = createChart(container, {
      width: container.offsetWidth,
      height,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: isDark ? '#a6adbb' : '#333',
      },
      grid: {
        vertLines: { visible: false },
        horzLines: { color: isDark ? 'rgba(166, 173, 187, 0.1)' : 'rgba(0, 0, 0, 0.08)' },
      },
      rightPriceScale: { borderColor: border },
      timeScale: { borderColor: border, timeVisible: false },
    })

    for (const s of series) {
      const line = chart.addSeries(LineSeries, {
        color: s.color,
        lineWidth: 2,
        priceFormat: {
          type: 'custom',
          formatter: (p: number) => `${signed && p > 0 ? '+' : ''}${p.toFixed(1)}R`,
        },
      })
      if (signed) {
        line.createPriceLine({ price: 0, color: border, lineWidth: 1, title: '0' })
      }
      line.setData(
        toLineData(s.points).map((p) => ({ time: p.time as UTCTimestamp, value: p.value }))
      )
    }
    chart.timeScale().fitContent()

    const handleResize = () => chart.applyOptions({ width: container.offsetWidth })
    window.addEventListener('resize', handleResize)
    return () => {
      window.removeEventListener('resize', handleResize)
      chart.remove()
    }
  }, [series, isDark, height, hasData, signed])

  return (
    <figure className="space-y-2" aria-label={title}>
      <figcaption className="text-sm font-medium">{title}</figcaption>
      {hasData ? (
        <div ref={containerRef} style={{ height }} data-testid="line-chart" />
      ) : (
        <div className="text-sm text-muted-foreground py-6">No series recorded for this run</div>
      )}
      {series.length > 1 && (
        <ul className="flex flex-wrap gap-4 text-xs text-muted-foreground">
          {series.map((s) => (
            <li key={s.label} className="flex items-center gap-2">
              <span
                className="inline-block h-0.5 w-5"
                style={{ backgroundColor: s.color }}
                aria-hidden="true"
              />
              {s.label}
            </li>
          ))}
        </ul>
      )}
    </figure>
  )
}
