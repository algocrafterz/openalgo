import type {
  ComparisonCurves,
  CurvePoint,
  PeriodRange,
  RunComparison,
  SeriesPoint,
} from '@/api/backtestResults'
import { fmtNum } from '@/lib/backtestResults'
import { type PeriodView, VIEW_LABEL } from './CompareHeader'
import { LineChart } from './LineChart'
import { MonthlyBars } from './MonthlyBars'

export const COLOR_A = '#3b82f6'
export const COLOR_B = '#f97316'

function toSeries(points: CurvePoint[], side: 'a' | 'b'): SeriesPoint[] {
  return points.flatMap((p) => {
    const v = p[side]
    return v === null ? [] : ([[p.x, v]] as SeriesPoint[])
  })
}

/** Labels the shared and extra sessions as strips, one per range as provided. */
export function SpanBand({
  common,
  incremental,
}: {
  common: PeriodRange | null
  incremental: PeriodRange[]
}) {
  if (!common && incremental.length === 0) return null
  const strip = (kind: string, r: PeriodRange, color: string, name: string) => (
    <span key={`${kind}-${r.start}-${r.end}`} data-strip={kind} className="flex items-center gap-2">
      <span
        className="inline-block h-2 w-8 rounded opacity-60"
        style={{ backgroundColor: color }}
        aria-hidden="true"
      />
      {name} {r.start} to {r.end}
    </span>
  )
  return (
    <div data-testid="span-band" className="flex flex-wrap gap-4 text-xs text-muted-foreground">
      {common && strip('common', common, '#64748b', 'Common period')}
      {incremental.map((r) => strip('incremental', r, '#a855f7', 'Incremental period'))}
    </div>
  )
}

function summary(points: CurvePoint[]): string {
  const n = (s: 'a' | 'b') => points.filter((p) => p[s] !== null).length
  const last = (s: 'a' | 'b') => {
    const v = [...points].reverse().find((p) => p[s] !== null)?.[s]
    return fmtNum(v)
  }
  return `${points.length} points. Run A ${n('a')} values, last ${last('a')}. Run B ${n('b')} values, last ${last('b')}.`
}

function DateChart({
  id,
  title,
  points: sel,
  curves,
  view,
  incrementalRun,
  signed,
}: {
  id: string
  title: string
  points: CurvePoint[]
  curves: ComparisonCurves
  view: PeriodView
  incrementalRun: 'a' | 'b' | null
  signed?: boolean
}) {
  const series = [
    ...(view === 'incremental' && incrementalRun === 'b'
      ? []
      : [{ label: 'Run A', color: COLOR_A, points: toSeries(sel, 'a') }]),
    ...(view === 'incremental' && incrementalRun === 'a'
      ? []
      : [{ label: 'Run B', color: COLOR_B, points: toSeries(sel, 'b') }]),
  ]
  return (
    <div className="space-y-1">
      <LineChart title={title} series={series} signed={signed} />
      <SpanBand common={curves.common} incremental={curves.incremental} />
      <p className="text-xs text-muted-foreground" data-testid={`summary-${id}`}>
        {summary(sel)}
      </p>
    </div>
  )
}

function Distribution({ bins, label }: { bins: ComparisonCurves['distribution']; label: string }) {
  const max = Math.max(1, ...bins.flatMap((b) => [b.a, b.b]))
  return (
    <div className="space-y-2">
      <h4 className="text-sm font-medium">Net R distribution ({label})</h4>
      <div className="flex items-end gap-1 h-28" aria-hidden="true">
        {bins.map((b) => (
          <div key={b.bin_start} className="relative flex-1 h-full">
            <div
              className="absolute bottom-0 left-0 w-full"
              style={{ height: `${(b.a / max) * 100}%`, backgroundColor: COLOR_A, opacity: 0.55 }}
            />
            <div
              className="absolute bottom-0 left-0 w-full"
              style={{ height: `${(b.b / max) * 100}%`, backgroundColor: COLOR_B, opacity: 0.55 }}
            />
          </div>
        ))}
      </div>
      <table className="sr-only">
        <caption>Net R distribution counts per bin</caption>
        <thead>
          <tr>
            <th scope="col">Bin (R)</th>
            <th scope="col">Run A</th>
            <th scope="col">Run B</th>
          </tr>
        </thead>
        <tbody>
          {bins.map((b) => (
            <tr key={b.bin_start}>
              <th scope="row">
                {fmtNum(b.bin_start)} to {fmtNum(b.bin_end)}
              </th>
              <td>{b.a}</td>
              <td>{b.b}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** The curve set for the selected view, or the full one when that view has no curves. */
export function curvesFor(
  comparison: RunComparison,
  view: PeriodView
): { curves: ComparisonCurves; shown: PeriodView } {
  const picked =
    view === 'common'
      ? comparison.curves_common
      : view === 'incremental'
        ? comparison.curves_incremental
        : null
  return picked ? { curves: picked, shown: view } : { curves: comparison.curves, shown: 'full' }
}

export function CompareCurves({
  comparison,
  view,
  incrementalRun,
}: {
  comparison: RunComparison
  view: PeriodView
  incrementalRun: 'a' | 'b' | null
}) {
  const { curves, shown } = curvesFor(comparison, view)
  const common = { curves, view: shown, incrementalRun }
  return (
    <div className="space-y-6">
      <p className="text-sm" data-testid="curves-title">
        {VIEW_LABEL[shown]}
      </p>
      {shown !== view && (
        <p className="text-xs text-muted-foreground">
          Curves for the {VIEW_LABEL[view].toLowerCase()} are not available; the full period is
          shown.
        </p>
      )}
      <ul className="flex gap-4 text-xs" aria-label="Legend">
        <li className="flex items-center gap-2">
          <span
            className="inline-block h-2 w-5"
            style={{ backgroundColor: COLOR_A }}
            aria-hidden="true"
          />
          Run A
        </li>
        <li className="flex items-center gap-2">
          <span
            className="inline-block h-2 w-5"
            style={{ backgroundColor: COLOR_B }}
            aria-hidden="true"
          />
          Run B
        </li>
      </ul>
      {shown !== 'full' && (
        <p className="text-xs text-muted-foreground">
          Curves start at zero at the start of this window.
        </p>
      )}
      <DateChart id="equity" title="Equity (R)" points={curves.equity_r} {...common} />
      <DateChart
        id="drawdown"
        title="Drawdown (R, zero or negative)"
        points={curves.drawdown_r}
        signed
        {...common}
      />
      <MonthlyBars points={curves.monthly} />
      <DateChart
        id="rolling"
        title={`Rolling mean net R per trade (${curves.rolling_window} trades)`}
        points={curves.rolling}
        {...common}
      />
      <Distribution bins={curves.distribution} label={VIEW_LABEL[shown].toLowerCase()} />
    </div>
  )
}
