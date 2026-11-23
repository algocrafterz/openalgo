import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { useMemo, useState } from 'react'
import {
  type DiscretionaryCategory,
  type DiscretionaryFilters,
  type DiscretionarySummary,
  discretionaryPnlApi,
} from '@/api/discretionaryPnl'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  type EquityRangeKey,
  equityRangeOptions,
  uncollectedDaysText,
  uncollectedInSpan,
} from '@/lib/discretionaryEquityRange'
import {
  DISCRETIONARY_POLL_MS,
  drawdownReconciliation,
  equityCurveBasis,
  equityCurveReconciliation,
  type NetCurve,
  netEquityFromDailyRows,
  type Reconciliation,
} from '@/lib/discretionaryPnlView'
import { fmtDate } from '@/lib/strategyPnlView'
import { DiscretionaryEquityChart } from './DiscretionaryEquityChart'

type Money = (value: number) => string

function spanText(start: string, end: string): string {
  return start === end ? fmtDate(start) : `${fmtDate(start)} to ${fmtDate(end)}`
}

/**
 * Equity Curve: the running total of the daily realised net P&L rows of one summary response.
 * By default it uses the report period the page already has. The range choices (1M, 3M, 6M, All)
 * ask the same summary endpoint for a custom range, with the same broker, account, symbol and
 * trade-category filters, and change only this card: the report date and every other figure on
 * the page stay as they are. A choice that reaches back before the first tracked date is
 * switched off, because it could not show that much history.
 */
export function EquityCurveCard({
  summary,
  filters,
  category,
  today,
  fallback,
  position,
  formatCurrency,
  isDarkMode,
}: {
  /** The summary of the report period the page shows. */
  summary: DiscretionarySummary
  filters: DiscretionaryFilters
  category: DiscretionaryCategory
  today: string
  /** Older servers without daily rows: the end-of-day curve built from the trades. */
  fallback: { curve: NetCurve; basis: string }
  /**
   * For 7 or fewer close dates the curve is drawn per closed trade instead of per day; it is the
   * one curve of the report period. Choosing another range switches to the day curve of it.
   */
  position?: { curve: NetCurve; basis: string; reconciliation: Reconciliation } | null
  formatCurrency: Money
  isDarkMode: boolean
}) {
  const [choice, setChoice] = useState<EquityRangeKey>('period')
  const fallbackCurve = fallback.curve
  const fallbackBasis = fallback.basis
  // The first tracked date the server reports; without it, the start of the data on screen.
  const since = summary.period.reliable_since ?? summary.period.start
  const pageStart = summary.period.start
  const pageEnd = summary.period.end
  const options = useMemo(() => {
    const all = equityRangeOptions({ today, since })
    // All that equals the report period adds nothing
    return all.filter(
      (o) => !(o.key === 'all' && o.range?.start === pageStart && o.range?.end === pageEnd)
    )
  }, [today, since, pageStart, pageEnd])
  const showChoices = options.some((o) => o.key !== 'period' && o.available)
  const active =
    showChoices && choice !== 'period'
      ? (options.find((o) => o.key === choice && o.available && o.range) ?? null)
      : null
  const range = active?.range ?? null

  const query = useQuery({
    queryKey: ['discretionary-pnl', 'equity-range', range?.start, range?.end, filters, category],
    queryFn: () =>
      discretionaryPnlApi.summary('custom', range ?? undefined, filters, false, category),
    enabled: !!range,
    refetchInterval: DISCRETIONARY_POLL_MS,
    placeholderData: keepPreviousData,
    retry: false,
  })
  // A range still loading must not show the previous range's curve under the new label.
  const ranged = range && query.data && !query.isPlaceholderData ? query.data : undefined
  const source = range ? ranged : summary

  const view = useMemo(() => {
    if (!source) return null
    const rows = source.by_period?.daily ?? []
    const start = source.period.start
    const end = source.period.end
    const uncollected = uncollectedInSpan(source.sync_health?.days_without_sync, start, end)
    const rowCurve = netEquityFromDailyRows(rows, uncollected, source.realized_net_of_charges)
    const hasRows = rowCurve.points.length >= 2
    const useFallback = !range && !hasRows && fallbackCurve.points.length > 0
    const curve = hasRows ? rowCurve : useFallback ? fallbackCurve : null
    const withFigure = new Set(rows.map((r) => r.period))
    return {
      rows,
      start,
      end,
      rowCurve,
      curve,
      hasRows,
      basis: hasRows ? equityCurveBasis(rowCurve) : fallbackBasis,
      uncollected,
      uncollectedNote: uncollectedDaysText(uncollected, withFigure),
    }
  }, [source, range, fallbackCurve, fallbackBasis])

  const separate = !!view && !!range && (view.start !== pageStart || view.end !== pageEnd)

  const positionMode = !!position && !range

  let content: React.ReactNode
  if (positionMode && position) {
    content =
      position.curve.points.length > 0 ? (
        <DiscretionaryEquityChart
          curve={position.curve}
          mode="position"
          reconciliation={position.reconciliation}
          basis={position.basis}
          isDarkMode={isDarkMode}
          formatCurrency={formatCurrency}
        />
      ) : (
        <>
          <p className="text-sm text-muted-foreground" data-testid="net-equity-empty">
            No completed trade with a net P&amp;L yet, so there is nothing to chart.
          </p>
          <p className="mt-2 text-xs text-muted-foreground" data-testid="net-equity-caption">
            {position.basis}
          </p>
        </>
      )
  } else if (range && !ranged) {
    content = query.isError ? (
      <div className="flex flex-wrap items-center gap-3 text-sm" data-testid="equity-range-error">
        <span>Could not load this range. The rest of the page is not affected.</span>
        <Button type="button" size="sm" variant="outline" onClick={() => query.refetch()}>
          Retry
        </Button>
      </div>
    ) : (
      <div className="flex items-center justify-center py-8" data-testid="equity-range-loading">
        <Loader2 className="size-6 animate-spin" />
      </div>
    )
  } else if (!view || !view.curve) {
    content = (
      <p className="text-sm text-muted-foreground" data-testid="equity-range-empty">
        {range
          ? 'Fewer than two trading days with P&L in this range.'
          : 'No completed trade with a net P&L yet.'}
      </p>
    )
  } else {
    const net = (range ? ranged : summary)?.realized_net_of_charges ?? 0
    const reconciliation = drawdownReconciliation({
      curve: view.curve,
      mode: 'day',
      tradesShown: source?.trades_shown ?? source?.trades.length ?? 0,
      tradesTotal: source?.trades_total ?? 0,
      risk: source?.risk,
      formatCurrency,
    })
    content = (
      <DiscretionaryEquityChart
        curve={view.curve}
        mode="day"
        reconciliation={reconciliation}
        netReconciliation={
          view.hasRows
            ? equityCurveReconciliation(
                view.rowCurve,
                net,
                formatCurrency,
                range ? 'this range' : 'the selected period'
              )
            : undefined
        }
        basis={view.basis}
        notCollectedDays={view.uncollected}
        uncollectedNote={view.uncollectedNote}
        isDarkMode={isDarkMode}
        formatCurrency={formatCurrency}
      />
    )
  }

  return (
    <Card data-testid="equity-curve-card">
      <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2">
        <CardTitle>{positionMode ? 'Trade-by-trade P&L' : 'Equity curve'}</CardTitle>
        {showChoices && (
          <fieldset
            className="flex flex-wrap items-center gap-1 border-0 p-0"
            aria-label="Equity curve range"
            data-testid="equity-range-choices"
          >
            {options.map((o) => (
              <Button
                key={o.key}
                type="button"
                size="sm"
                variant={(active?.key ?? 'period') === o.key ? 'secondary' : 'ghost'}
                aria-pressed={(active?.key ?? 'period') === o.key}
                disabled={!o.available}
                title={o.reason ?? undefined}
                data-testid={`equity-range-${o.key}`}
                onClick={() => setChoice(o.key)}
              >
                {o.label}
              </Button>
            ))}
          </fieldset>
        )}
      </CardHeader>
      <CardContent>
        {view && (!range || ranged) && !positionMode && (
          <p className="mb-2 text-xs text-muted-foreground" data-testid="equity-range-label">
            {range ? 'Range shown' : 'Report period'}: {spanText(view.start, view.end)}.
            {separate ? ` Differs from the report period (${spanText(pageStart, pageEnd)}).` : ''}
          </p>
        )}
        {content}
      </CardContent>
    </Card>
  )
}
