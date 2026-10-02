import type { MetricDelta, RunComparison, RunMetrics } from '@/api/backtestResults'
import { fmtNum, fmtPercent, fmtSigned, METRIC_LABELS, NA } from '@/lib/backtestResults'
import type { PeriodView } from './CompareHeader'

const COUNT_KEYS = new Set(['trades', 'days'])

function fmtValue(key: string, v: number | null | undefined): string {
  if (v === null || v === undefined) return NA
  if (key === 'win_rate') return fmtPercent(v)
  return COUNT_KEYS.has(key) ? String(v) : fmtNum(v)
}

function fmtDelta(key: string, d: number | null | undefined): string {
  if (d === null || d === undefined) return NA
  if (key === 'win_rate') return `${fmtSigned(d * 100, 1)} pts`
  return COUNT_KEYS.has(key) ? fmtSigned(d, 0) : fmtSigned(d)
}

function orderKeys(keys: string[]): string[] {
  const known = Object.keys(METRIC_LABELS).filter((k) => keys.includes(k))
  return [...known, ...keys.filter((k) => !(k in METRIC_LABELS)).sort()]
}

const TH = 'text-left font-medium py-1 pr-4'
const NUM = 'py-1 pr-4 font-mono text-right'

interface Row {
  key: string
  a: number | null | undefined
  b: number | null | undefined
  delta: number | null | undefined
}

function fromMetrics(a: RunMetrics, b: RunMetrics): Row[] {
  const keys = orderKeys([...new Set([...Object.keys(a), ...Object.keys(b)])])
  return keys.map((key) => ({ key, a: a[key], b: b[key], delta: undefined }))
}

function fromDeltas(perf: Record<string, MetricDelta>): Row[] {
  return orderKeys(Object.keys(perf)).map((key) => ({ key, ...perf[key] }))
}

/** Metric table for the selected period view. Deltas are the backend's; never judged. */
export function ComparePerformance({
  comparison,
  view,
}: {
  comparison: RunComparison
  view: PeriodView
}) {
  const { views } = comparison
  if (view === 'incremental' && views.incremental) {
    const inc = views.incremental
    const rows = Object.keys(inc.metrics)
    return (
      <div className="space-y-2">
        <p className="text-sm">
          Run {inc.run.toUpperCase()} only, over{' '}
          <span data-testid="incremental-range">
            {inc.ranges.map((r) => `${r.start} to ${r.end}`).join(', ')}
          </span>
        </p>
        <table className="w-full text-sm">
          <caption className="sr-only">Performance metrics</caption>
          <thead>
            <tr>
              <th scope="col" className={TH}>
                Metric
              </th>
              <th scope="col" className={`${TH} text-right`}>
                Run {inc.run.toUpperCase()}
              </th>
            </tr>
          </thead>
          <tbody>
            {orderKeys(rows).map((k) => (
              <tr key={k} className="border-t">
                <th scope="row" className="text-left py-1 pr-4 font-normal">
                  {METRIC_LABELS[k] ?? k}
                </th>
                <td className={NUM}>{fmtValue(k, inc.metrics[k])}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }
  const common = view === 'common' ? views.common : null
  const rows = common
    ? comparison.performance_common
      ? fromDeltas(comparison.performance_common)
      : fromMetrics(common.a, common.b)
    : fromDeltas(comparison.performance)
  return (
    <div className="space-y-2">
      {common && <p className="text-xs text-muted-foreground">Common sessions only.</p>}
      <table className="w-full text-sm">
        <caption className="sr-only">Performance metrics</caption>
        <thead>
          <tr>
            <th scope="col" className={TH}>
              Metric
            </th>
            <th scope="col" className={`${TH} text-right`}>
              Run A
            </th>
            <th scope="col" className={`${TH} text-right`}>
              Run B
            </th>
            <th scope="col" className={`${TH} text-right`}>
              Delta (B minus A)
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} className="border-t">
              <th scope="row" className="text-left py-1 pr-4 font-normal">
                {METRIC_LABELS[r.key] ?? r.key}
              </th>
              <td className={NUM}>{fmtValue(r.key, r.a)}</td>
              <td className={NUM}>{fmtValue(r.key, r.b)}</td>
              <td className={`${NUM} text-muted-foreground`} data-delta={r.key}>
                {fmtDelta(r.key, r.delta)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
