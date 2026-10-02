import type { CurvePoint } from '@/api/backtestResults'
import { fmtSigned, NA } from '@/lib/backtestResults'

const COLOR_A = '#3b82f6'
const COLOR_B = '#f97316'

/** Net R per month for both runs, as paired bars with an exact-value table for screen readers. */
export function MonthlyBars({ points }: { points: CurvePoint[] }) {
  if (points.length === 0) {
    return <div className="text-sm text-muted-foreground">No monthly values in this period</div>
  }
  const max = Math.max(0.0001, ...points.flatMap((p) => [Math.abs(p.a ?? 0), Math.abs(p.b ?? 0)]))
  const bar = (v: number | null, color: string) =>
    v === null ? (
      <div className="h-3" />
    ) : (
      <div
        className="h-3"
        style={{ width: `${Math.max((Math.abs(v) / max) * 100, 1)}%`, backgroundColor: color }}
      />
    )
  return (
    <figure className="space-y-1" aria-label="Monthly net R">
      <figcaption className="text-sm font-medium">Monthly net R</figcaption>
      <table className="w-full text-xs">
        <caption className="sr-only">Net R per month, run A and run B</caption>
        <thead>
          <tr>
            <th scope="col" className="text-left font-medium">
              Month
            </th>
            <th scope="col" className="text-right font-medium">
              Run A
            </th>
            <th scope="col" className="text-right font-medium">
              Run B
            </th>
            <th scope="col" className="w-1/2 font-medium text-left">
              Bars (length is absolute size)
            </th>
          </tr>
        </thead>
        <tbody>
          {points.map((p) => (
            <tr key={p.x}>
              <th scope="row" className="text-left font-mono font-normal">
                {p.x}
              </th>
              <td className="text-right font-mono">{p.a === null ? NA : fmtSigned(p.a, 1)}</td>
              <td className="text-right font-mono">{p.b === null ? NA : fmtSigned(p.b, 1)}</td>
              <td aria-hidden="true">
                {bar(p.a, COLOR_A)}
                {bar(p.b, COLOR_B)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  )
}
