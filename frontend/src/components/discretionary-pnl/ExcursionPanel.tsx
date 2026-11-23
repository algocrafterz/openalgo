import { useMemo } from 'react'
import type { ExcursionDist, ExcursionsResponse } from '@/api/discretionaryPnl'
import { Button } from '@/components/ui/button'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import {
  coverageText,
  excursionReasonPhrases,
  fmtDistance,
  scatterPoints,
} from '@/lib/discretionaryPnlView'
import { MaeMfeScatter } from './DiscretionaryCharts'

type Money = (value: number) => string

function cell(
  dist: ExcursionDist | undefined,
  key: 'average' | 'median',
  fmt: (v: number) => string
) {
  const v = dist && dist.count > 0 ? dist[key] : null
  return v == null ? '—' : fmt(v)
}

/**
 * MAE / MFE of the closed positions: how far a trade went against you and for you before it
 * closed. Loaded after the page, so it can lag behind the figures above. Every aggregate says how
 * many positions it rests on out of the total; positions without price data are left out and
 * counted, never shown as zero. Profit capture (a ratio to a lower-bound MFE that can pass 100%)
 * is not shown.
 */
export function ExcursionPanel({
  data,
  loading,
  failed,
  onRetry,
  formatCurrency,
}: {
  data: ExcursionsResponse | undefined
  loading: boolean
  failed: boolean
  onRetry: () => void
  formatCurrency: Money
}) {
  const points = useMemo(() => scatterPoints(data?.positions), [data])
  const reasons = useMemo(() => {
    const text = new Map<string, string>()
    for (const p of data?.positions ?? []) {
      if (p.reason && p.reason_text) text.set(p.reason, p.reason_text)
    }
    return excursionReasonPhrases(data?.aggregates.coverage.unavailable_reasons, text)
  }, [data])

  if (!data) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="excursions-status">
        {failed ? (
          <>
            MAE / MFE could not be loaded.{' '}
            <Button type="button" size="sm" variant="outline" onClick={onRetry}>
              Retry
            </Button>
          </>
        ) : loading ? (
          'Loading MAE / MFE...'
        ) : (
          'MAE / MFE is not loaded yet.'
        )}
      </p>
    )
  }
  const agg = data.aggregates
  const w = agg.winners
  const l = agg.losers
  // Nothing to average or plot: one plain line, not a table of dashes and an empty chart.
  if (agg.coverage.available === 0) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="excursions-none">
        MAE / MFE is not shown: {coverageText(0, agg.coverage.total)}
        {reasons.length > 0 ? ` (${reasons.join('; ')})` : ''}.
      </p>
    )
  }
  const rupees = (v: number) => formatCurrency(v)
  const pts = (v: number) => fmtDistance(v)
  const rows: {
    label: string
    unit: string
    w: ExcursionDist
    l: ExcursionDist
    fmt: (v: number) => string
  }[] = [
    {
      label: 'MAE (worst move against you)',
      unit: 'points per unit',
      w: w.mae_points,
      l: l.mae_points,
      fmt: pts,
    },
    {
      label:
        'MFE: largest favourable price move while open (market measurement, not realisable P&L)',
      unit: 'points per unit',
      w: w.mfe_points,
      l: l.mfe_points,
      fmt: pts,
    },
    { label: 'MAE', unit: 'Rs', w: w.mae_amount, l: l.mae_amount, fmt: rupees },
    { label: 'MFE', unit: 'Rs', w: w.mfe_amount, l: l.mfe_amount, fmt: rupees },
  ]
  return (
    <div className="space-y-3" data-testid="excursion-panel">
      <p className="text-xs" data-testid="excursions-coverage">
        <span className="font-medium">
          Based on {agg.coverage.available} of {agg.coverage.total} completed positions with
          1-minute price data
        </span>
        <span className="text-muted-foreground">
          {' '}
          (bounds from minute bars, not exact
          {agg.coverage.available < agg.coverage.total
            ? '; the other positions are not included'
            : ''}
          )
        </span>
        {reasons.length > 0 && (
          <span className="text-muted-foreground"> (unavailable: {reasons.join('; ')})</span>
        )}
      </p>
      <div className="min-w-0 overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Per position</TableHead>
              <TableHead className="text-right">
                Winners average ({w.mae_points.count} positions)
              </TableHead>
              <TableHead className="text-right">Winners median</TableHead>
              <TableHead className="text-right">
                Losers average ({l.mae_points.count} positions)
              </TableHead>
              <TableHead className="text-right">Losers median</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((r) => (
              <TableRow key={`${r.label}-${r.unit}`} data-testid="excursion-row">
                <TableCell className="font-medium">
                  {r.label}{' '}
                  <span className="text-xs font-normal text-muted-foreground">{r.unit}</span>
                </TableCell>
                <TableCell className="text-right font-mono">
                  {cell(r.w, 'average', r.fmt)}
                </TableCell>
                <TableCell className="text-right font-mono">{cell(r.w, 'median', r.fmt)}</TableCell>
                <TableCell className="text-right font-mono">
                  {cell(r.l, 'average', r.fmt)}
                </TableCell>
                <TableCell className="text-right font-mono">{cell(r.l, 'median', r.fmt)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      <p className="text-xs text-muted-foreground" data-testid="excursions-precision">
        {agg.precision_note}
      </p>
      {points.length >= 2 && <MaeMfeScatter points={points} formatCurrency={formatCurrency} />}
    </div>
  )
}
