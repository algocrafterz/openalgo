import { useMemo } from 'react'
import type {
  ExcursionDist,
  ExcursionsResponse,
  InstrumentCapture,
  InstrumentExcursions,
} from '@/api/discretionaryPnl'
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
  availableInstrumentCount,
  captureExclusionPhrases,
  coverageText,
  excursionReasonPhrases,
  fmtCapture,
  fmtDistance,
  instrumentLabel,
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

interface PointRow {
  label: string
  unit: string
  w: ExcursionDist
  l: ExcursionDist
  fmt: (v: number) => string
}

function PointsTable({
  title,
  rows,
  wCount,
  lCount,
  testId = 'excursion-table',
}: {
  title: string
  rows: PointRow[]
  wCount: number
  lCount: number
  testId?: string
}) {
  if (rows.length === 0) return null
  return (
    <div className="min-w-0 overflow-x-auto" data-testid={testId}>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>{title}</TableHead>
            <TableHead className="text-right">Winners average ({wCount} positions)</TableHead>
            <TableHead className="text-right">Winners median</TableHead>
            <TableHead className="text-right">Losers average ({lCount} positions)</TableHead>
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
              <TableCell className="text-right font-mono">{cell(r.w, 'average', r.fmt)}</TableCell>
              <TableCell className="text-right font-mono">{cell(r.w, 'median', r.fmt)}</TableCell>
              <TableCell className="text-right font-mono">{cell(r.l, 'average', r.fmt)}</TableCell>
              <TableCell className="text-right font-mono">{cell(r.l, 'median', r.fmt)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

function shareText(half: InstrumentCapture['mae_over_half']): string {
  if (half.denominator === 0) return '—'
  const pct = half.share == null ? '' : ` (${(half.share * 100).toFixed(0)}%)`
  return `${half.count} of ${half.denominator}${pct}`
}

/**
 * Capture and giveback per instrument. Every figure sits beside its sample size (n of N): N is the
 * completed positions of the instrument, n those that entered the figure. No pooled row.
 */
function CaptureTable({ instruments }: { instruments: InstrumentExcursions[] }) {
  return (
    <div className="space-y-1" data-testid="capture-by-instrument">
      <div className="min-w-0 overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Capture by instrument</TableHead>
              <TableHead className="text-right">Positions counted</TableHead>
              <TableHead className="text-right">Median capture</TableHead>
              <TableHead className="text-right">Median giveback (points)</TableHead>
              <TableHead className="text-right">MAE above half of MFE</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {instruments.map((inst) => {
              const c = inst.capture
              const some = c.captured > 0
              return (
                <TableRow key={`${inst.symbol}|${inst.exchange}`} data-testid="capture-row">
                  <TableCell className="font-medium">
                    {instrumentLabel(inst.symbol, inst.exchange)}
                  </TableCell>
                  <TableCell className="text-right font-mono" data-testid="capture-n">
                    n = {c.captured} of {c.positions}
                  </TableCell>
                  <TableCell className="text-right font-mono" data-testid="capture-median">
                    {some ? fmtCapture(c.median_capture) : '—'}
                  </TableCell>
                  <TableCell className="text-right font-mono" data-testid="giveback-median">
                    {some ? fmtDistance(c.median_giveback_points) : '—'}
                  </TableCell>
                  <TableCell className="text-right font-mono" data-testid="mae-half">
                    {shareText(c.mae_over_half)}
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </div>
      {instruments
        .filter((i) => i.capture.excluded_total > 0)
        .map((i) => (
          <p
            key={`${i.symbol}|${i.exchange}`}
            className="text-xs text-muted-foreground"
            data-testid="capture-excluded"
          >
            {instrumentLabel(i.symbol, i.exchange)}, not counted:{' '}
            {captureExclusionPhrases(i.capture.excluded).join('; ')}
          </p>
        ))}
      <p className="text-xs text-muted-foreground" data-testid="capture-basis">
        Capture = realised points / MFE; giveback = MFE minus realised points. Gross points per unit
        on the average entry in force at each exit; charges are not deducted from points. Only net
        winners (after charges) with at least one full candle between the first and last fill and
        MFE above 0 are counted. MAE above half of MFE: positions with such candle coverage and MFE
        above 0. The counts show how many positions each figure rests on.
      </p>
    </div>
  )
}

/**
 * MAE / MFE of the closed positions: how far a trade went against you and for you before it
 * closed. Loaded after the page, so it can lag behind the figures above. Every aggregate says how
 * many positions it rests on out of the total; positions without price data are left out and
 * counted, never shown as zero. MAE and MFE are conservative lower bounds. Points are never pooled
 * across instruments: with more than one instrument they are shown per instrument. Capture and
 * giveback are shown per instrument only, with n of N positions beside each figure.
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
  const instruments = agg.instruments
  const multi = availableInstrumentCount(data.positions) > 1 || w.mae_points == null
  const only = data.positions.find((p) => p.status === 'available')
  const rupeeRows: PointRow[] = [
    { label: 'MAE', unit: 'Rs', w: w.mae_amount, l: l.mae_amount, fmt: rupees },
    { label: 'MFE', unit: 'Rs', w: w.mfe_amount, l: l.mfe_amount, fmt: rupees },
  ]
  const pointRows = (
    wg: { mae_points: ExcursionDist | null; mfe_points: ExcursionDist | null },
    lg: { mae_points: ExcursionDist | null; mfe_points: ExcursionDist | null }
  ): PointRow[] =>
    wg.mae_points && wg.mfe_points && lg.mae_points && lg.mfe_points
      ? [
          {
            label: 'MAE (worst move against you)',
            unit: 'points per unit, lower bound',
            w: wg.mae_points,
            l: lg.mae_points,
            fmt: pts,
          },
          {
            label:
              'MFE: largest favourable price move while open (market measurement, not realisable P&L)',
            unit: 'points per unit, lower bound',
            w: wg.mfe_points,
            l: lg.mfe_points,
            fmt: pts,
          },
        ]
      : []
  return (
    <div className="space-y-3" data-testid="excursion-panel">
      <p className="text-xs" data-testid="excursions-coverage" title={agg.precision_note}>
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
      <p className="text-xs text-muted-foreground" data-testid="excursions-lower-bound">
        MAE and MFE are conservative lower bounds: the minute that holds a fill uses the fill price,
        not that candle's high or low.
      </p>
      {multi ? (
        <>
          <PointsTable
            title="Per position, all instruments"
            rows={rupeeRows}
            wCount={w.mae_amount.count}
            lCount={l.mae_amount.count}
          />
          <p
            className="text-xs text-muted-foreground"
            data-testid="excursions-points-per-instrument"
          >
            Points are shown per instrument, never added across instruments.
          </p>
          {(instruments ?? []).map((inst) => (
            <PointsTable
              key={`${inst.symbol}|${inst.exchange}`}
              title={`Points per position, ${instrumentLabel(inst.symbol, inst.exchange)}`}
              rows={pointRows(inst.winners, inst.losers)}
              wCount={inst.winners.mae_points.count}
              lCount={inst.losers.mae_points.count}
              testId="instrument-points"
            />
          ))}
        </>
      ) : (
        <PointsTable
          title={`Per position${only ? `, ${instrumentLabel(only.symbol, only.exchange)}` : ''}`}
          rows={[...pointRows(w, l), ...rupeeRows]}
          wCount={w.mae_points?.count ?? 0}
          lCount={l.mae_points?.count ?? 0}
        />
      )}
      {instruments && instruments.length > 0 && <CaptureTable instruments={instruments} />}
      {!multi && points.length >= 2 && (
        <MaeMfeScatter points={points} formatCurrency={formatCurrency} />
      )}
    </div>
  )
}
