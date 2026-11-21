import { useMemo } from 'react'
import {
  type CurvePoint,
  fmtStamp,
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
  const unit = mode === 'position' ? 'closed position' : 'day'
  return `${pts.length} ${unit}${pts.length === 1 ? '' : 's'}. Cumulative net P&L: first ${money(eq[0])}, last ${money(eq[eq.length - 1])}, lowest ${money(Math.min(...eq))}, highest ${money(Math.max(...eq))}. Maximum drawdown ${money(curve.maxDrawdown)}, current drawdown ${money(curve.currentDrawdown)}.`
}

function DataTable({
  points,
  mode,
  money,
}: {
  points: CurvePoint[]
  mode: 'position' | 'day'
  money: Money
}) {
  return (
    <details className="mt-2 text-xs" data-testid="equity-data-table">
      <summary className="cursor-pointer text-muted-foreground">
        Show the {points.length} point(s) as a table
      </summary>
      <div className="mt-1 max-h-64 overflow-auto rounded border">
        <table className="w-full min-w-[34rem] text-left">
          <thead className="sticky top-0 bg-muted">
            <tr>
              <th className="px-2 py-1">{mode === 'position' ? 'Position' : 'Day'}</th>
              {mode === 'position' && <th className="px-2 py-1">Closed</th>}
              {mode === 'position' && <th className="px-2 py-1">Symbol</th>}
              <th className="px-2 py-1 text-right">Net P&amp;L</th>
              <th className="px-2 py-1 text-right">Cumulative net</th>
              <th className="px-2 py-1 text-right">Drawdown</th>
            </tr>
          </thead>
          <tbody>
            {points.map((p) => (
              <tr key={p.index} className="border-t">
                <td className="px-2 py-1 font-mono">
                  {mode === 'position' ? p.index : p.closed_at}
                </td>
                {mode === 'position' && <td className="px-2 py-1">{fmtStamp(p.closed_at)}</td>}
                {mode === 'position' && (
                  <td className="px-2 py-1">
                    {p.symbol} {p.direction === 'LONG' ? 'Long' : 'Short'}
                  </td>
                )}
                <td className="px-2 py-1 text-right font-mono">{money(p.net)}</td>
                <td className="px-2 py-1 text-right font-mono">{money(p.equity)}</td>
                <td className="px-2 py-1 text-right font-mono">{money(p.drawdown)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  )
}

/**
 * Net equity and drawdown of the closed positions. Per position (x = position number in close
 * order) for a session or a short range, end-of-day for a long one. The caption states the
 * basis and whether the curve's maximum drawdown equals the Risk card.
 */
export function DiscretionaryEquityChart({
  curve,
  mode,
  reconciliation,
  basis,
  isDarkMode,
  formatCurrency,
}: {
  curve: NetCurve
  mode: 'position' | 'day'
  reconciliation: Reconciliation
  /** Basis sentence, e.g. net realised per closed position by close time. */
  basis: string
  isDarkMode: boolean
  formatCurrency: Money
}) {
  const summaryText = useMemo(
    () => curveSummaryText(curve, mode, formatCurrency),
    [curve, mode, formatCurrency]
  )
  const n = curve.points.length
  return (
    <div data-testid="equity-chart-block">
      <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block h-0.5 w-4 bg-green-600" aria-hidden="true" />
          <span className="inline-block h-0.5 w-4 bg-red-600" aria-hidden="true" />
          Cumulative net P&amp;L (green above 0, red below), upper pane
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span
            className="inline-block h-2 w-4 rounded-sm"
            style={{ background: DRAWDOWN_COLOR }}
            aria-hidden="true"
          />
          Drawdown below the peak, lower pane
        </span>
        <span>Amounts in rupees (Rs)</span>
      </div>
      <div
        role="img"
        aria-label={`${mode === 'position' ? 'Equity per closed position' : 'End-of-day equity'}. ${summaryText}`}
      >
        <EquityCanvas
          points={curve.points}
          mode={mode}
          isDarkMode={isDarkMode}
          formatCurrency={formatCurrency}
        />
      </div>
      <p className="mt-1 text-[11px] text-muted-foreground" data-testid="equity-axis-note">
        {mode === 'position'
          ? 'Horizontal axis: closed position number in close order. Close times are in the hover and the table; positions closed in the same second keep their order.'
          : 'Horizontal axis: close date.'}
      </p>
      {n <= 3 && (
        <p className="text-xs text-muted-foreground" data-testid="equity-few-note">
          Only {n} point{n === 1 ? '' : 's'}: each is marked on the line.
        </p>
      )}
      {/* biome-ignore lint/a11y/noNoninteractiveTabindex: lets a keyboard user reach the values */}
      <p className="mt-2 text-xs" tabIndex={0} data-testid="equity-text-summary">
        {summaryText}
      </p>
      <p className="mt-1 text-xs text-muted-foreground" data-testid="net-equity-caption">
        {basis}
      </p>
      <p
        className={cn(
          'mt-1 text-xs',
          reconciliation.kind === 'match' ? 'text-muted-foreground' : 'text-amber-600'
        )}
        data-testid="equity-reconciliation"
        data-kind={reconciliation.kind}
      >
        {reconciliation.text}
      </p>
      <DataTable points={curve.points} mode={mode} money={formatCurrency} />
    </div>
  )
}
