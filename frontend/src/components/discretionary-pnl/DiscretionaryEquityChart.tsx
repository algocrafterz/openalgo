import { useMemo } from 'react'
import {
  EQUITY_CAPTION_DETAIL,
  type EquityReconciliation,
  type NetCurve,
  type Reconciliation,
} from '@/lib/discretionaryPnlView'
import { cn } from '@/lib/utils'
import { DRAWDOWN_COLOR, EquityCanvas } from './EquityCanvas'

type Money = (value: number) => string

/** First / last / lowest / highest cumulative net of a curve, for the text alternative. */
export function curveSummaryText(curve: NetCurve, mode: 'position' | 'day', money: Money): string {
  const pts = curve.points
  if (pts.length === 0) return 'No points.'
  const eq = pts.map((p) => p.equity)
  const unit = mode === 'position' ? 'closed trade' : 'day'
  return `${pts.length} ${unit}${pts.length === 1 ? '' : 's'}. Cumulative net P&L: first ${money(eq[0])}, last ${money(eq[eq.length - 1])}, lowest ${money(Math.min(...eq))}, highest ${money(Math.max(...eq))}.`
}

/**
 * Trade-by-trade P&L: the running total of net realised P&L (after estimated charges) and its
 * fall below the highest point so far. Per trade (x = trade number in close order) for a session
 * or a short range (7 or fewer close dates), end-of-day for a longer one. The maximum and current
 * drawdown are stated once, in the summary's key figures; the lower pane only draws the fall. The
 * individual trades are in Closed trades only, so there is no second table here.
 */
export function DiscretionaryEquityChart({
  curve,
  mode,
  reconciliation,
  netReconciliation,
  basis,
  notCollectedDays,
  uncollectedNote,
  isDarkMode,
  formatCurrency,
}: {
  curve: NetCurve
  mode: 'position' | 'day'
  reconciliation: Reconciliation
  /** Equity Curve only: ties the last point to the period's realised net P&L. */
  netReconciliation?: EquityReconciliation
  /** Basis sentence, e.g. net realised per closed position by close time. */
  basis: string
  /** Day curve: weekdays whose broker trades were not collected (drawn as breaks). */
  notCollectedDays?: string[]
  /** Day curve: the plain-language flag for those days. */
  uncollectedNote?: string | null
  isDarkMode: boolean
  formatCurrency: Money
}) {
  const summaryText = useMemo(
    () => curveSummaryText(curve, mode, formatCurrency),
    [curve, mode, formatCurrency]
  )
  const unit = mode === 'position' ? 'trade' : 'day'
  return (
    <div data-testid="equity-chart-block">
      <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block h-0.5 w-4 bg-green-600" aria-hidden="true" />
          <span className="inline-block h-0.5 w-4 bg-red-600" aria-hidden="true" />
          Cumulative net P&amp;L (green above 0, red below)
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span
            className="inline-block h-2 w-4 rounded-sm"
            style={{ background: DRAWDOWN_COLOR }}
            aria-hidden="true"
          />
          {mode === 'day'
            ? 'Drawdown, end-of-day totals (lower pane)'
            : 'Drawdown from the highest point (lower pane)'}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block size-2 rounded-full bg-green-600" aria-hidden="true" />
          <span className="inline-block size-2 rounded-full bg-red-600" aria-hidden="true" />
          One dot per {unit}: green win, red loss
        </span>
        <span>Amounts in rupees (Rs)</span>
      </div>
      <div
        role="img"
        aria-label={`${mode === 'position' ? 'Trade-by-trade P&L' : 'End-of-day net P&L'}. ${summaryText}`}
      >
        <EquityCanvas
          points={curve.points}
          mode={mode}
          notCollectedDays={notCollectedDays}
          isDarkMode={isDarkMode}
          formatCurrency={formatCurrency}
        />
      </div>
      <p
        className="mt-1 text-[11px] text-muted-foreground"
        data-testid="equity-axis-note"
        title={EQUITY_CAPTION_DETAIL}
      >
        {mode === 'position'
          ? 'Axis: trade number in close order.'
          : 'Axis: dates, weekends left out. A day with no dot had no trades.'}
      </p>
      {mode === 'day' && uncollectedNote && (
        <p
          className="mt-1 text-xs text-amber-700 dark:text-amber-500 [.analyzer_&]:text-amber-500"
          data-testid="equity-uncollected-days"
        >
          {uncollectedNote} An amber dot marks a day whose figure is incomplete.
        </p>
      )}
      {/* biome-ignore lint/a11y/noNoninteractiveTabindex: lets a keyboard user reach the values */}
      <p className="sr-only" tabIndex={0} data-testid="equity-text-summary">
        {summaryText}
      </p>
      <p
        className="mt-1 text-xs text-muted-foreground"
        data-testid="net-equity-caption"
        title={EQUITY_CAPTION_DETAIL}
      >
        {basis}
      </p>
      {netReconciliation && netReconciliation.kind === 'mismatch' && (
        <p
          className="mt-1 text-xs text-amber-700 dark:text-amber-500 [.analyzer_&]:text-amber-500"
          data-testid="equity-net-reconciliation"
          data-kind={netReconciliation.kind}
        >
          {netReconciliation.text}
        </p>
      )}
      {reconciliation.text && (
        // only a difference between the chart and the Risk figure is worth a line
        <p
          className={cn(
            'mt-1 text-xs',
            reconciliation.kind === 'day_curve'
              ? 'text-muted-foreground'
              : 'text-amber-700 dark:text-amber-500 [.analyzer_&]:text-amber-500'
          )}
          data-testid="equity-reconciliation"
          data-kind={reconciliation.kind}
        >
          {reconciliation.text}
        </p>
      )}
    </div>
  )
}
