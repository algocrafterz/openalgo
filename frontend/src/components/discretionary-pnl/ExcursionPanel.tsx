import { Info } from 'lucide-react'
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
import { fmtDistance, fmtPct, scatterPoints } from '@/lib/discretionaryPnlView'
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
 * closed. Loaded after the page, so it can lag behind the figures above. Positions without
 * price data show a dash and a reason, never zero.
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
    return Object.entries(data?.aggregates.coverage.unavailable_reasons ?? {}).map(
      ([code, n]) => `${n} ${text.get(code) ?? code.replace(/_/g, ' ')}`
    )
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
  const capture = w.profit_capture
  return (
    <div className="space-y-3" data-testid="excursion-panel">
      <p
        className="flex items-start gap-1.5 text-xs text-muted-foreground"
        data-testid="excursions-note"
      >
        <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        <span>{data.note}</span>
      </p>
      <p className="text-xs" data-testid="excursions-coverage">
        <span className="font-medium">{agg.coverage.label}</span>
        {reasons.length > 0 && (
          <span className="text-muted-foreground"> (unavailable: {reasons.join('; ')})</span>
        )}
      </p>
      <div className="min-w-0 overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Per position</TableHead>
              <TableHead className="text-right">Winners average ({w.mae_points.count})</TableHead>
              <TableHead className="text-right">Winners median</TableHead>
              <TableHead className="text-right">Losers average ({l.mae_points.count})</TableHead>
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
            <TableRow data-testid="profit-capture-row">
              <TableCell className="font-medium">
                Profit capture{' '}
                <span className="text-xs font-normal text-muted-foreground">
                  ({capture.count} winner(s))
                </span>
              </TableCell>
              <TableCell className="text-right font-mono">
                {cell(capture, 'average', (v) => fmtPct(v * 100))}
              </TableCell>
              <TableCell className="text-right font-mono">
                {cell(capture, 'median', (v) => fmtPct(v * 100))}
              </TableCell>
              <TableCell className="text-right text-muted-foreground">—</TableCell>
              <TableCell className="text-right text-muted-foreground">—</TableCell>
            </TableRow>
          </TableBody>
        </Table>
      </div>
      <div
        className="rounded-md border p-3 text-sm"
        data-testid="profit-capture-block"
        title="Profit capture = gross points a winner made / its MFE points (winners only). It can pass 100% because MFE is a lower bound."
      >
        {capture.count > 0 && capture.average != null ? (
          <span>
            Winners captured{' '}
            <span className="font-mono font-semibold">{fmtPct(capture.average * 100)}</span> of the
            favourable move (minute bars, lower bound) (median{' '}
            <span className="font-mono">
              {capture.median != null ? fmtPct(capture.median * 100) : '—'}
            </span>
            , {capture.count} position{capture.count === 1 ? '' : 's'}).
          </span>
        ) : (
          <span>
            Winners captured <span className="font-mono font-semibold">—</span> of the favourable
            move (minute bars, lower bound):{' '}
            <span className="text-muted-foreground">
              no winning position has both gross points and a favourable move to compare.
            </span>
          </span>
        )}
      </div>
      <p className="text-xs text-muted-foreground" data-testid="excursions-precision">
        {agg.precision_note} Profit capture is the points a winner made divided by its MFE; it can
        pass 100% because MFE is a lower bound.
      </p>
      <MaeMfeScatter points={points} formatCurrency={formatCurrency} />
    </div>
  )
}
