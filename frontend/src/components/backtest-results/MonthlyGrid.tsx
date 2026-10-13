import type { MonthlyRow } from '@/api/backtestResults'
import { fmtSigned } from '@/lib/backtestResults'

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

function cellStyle(value: number, maxAbs: number): React.CSSProperties {
  if (value === 0 || maxAbs === 0) return {}
  const alpha = 0.12 + 0.5 * Math.min(Math.abs(value) / maxAbs, 1)
  const rgb = value > 0 ? '34, 197, 94' : '239, 68, 68'
  return { backgroundColor: `rgba(${rgb}, ${alpha.toFixed(2)})` }
}

/**
 * One value per calendar month (net R by default). Shows whether a result is spread out or one
 * good month. `caption` and `cellTitle` name another unit, such as the book's monthly return.
 */
export function MonthlyGrid({
  rows,
  caption = 'Total net R per month',
  cellTitle = (row: MonthlyRow) => `${row.n} trades`,
}: {
  rows: MonthlyRow[]
  caption?: string
  cellTitle?: (row: MonthlyRow) => string
}) {
  if (rows.length === 0) {
    return <div className="text-sm text-muted-foreground">No monthly breakdown recorded</div>
  }
  const byKey = new Map(rows.map((r) => [r.month, r]))
  const years = [...new Set(rows.map((r) => r.month.slice(0, 4)))].sort()
  const maxAbs = Math.max(...rows.map((r) => Math.abs(r.total_r)))

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs border-separate border-spacing-1">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr>
            <th scope="col" className="text-left font-medium">
              Year
            </th>
            {MONTHS.map((m) => (
              <th key={m} scope="col" className="font-medium text-muted-foreground">
                {m}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {years.map((year) => (
            <tr key={year}>
              <th scope="row" className="text-left font-medium">
                {year}
              </th>
              {MONTHS.map((_, idx) => {
                const row = byKey.get(`${year}-${String(idx + 1).padStart(2, '0')}`)
                return (
                  <td
                    key={idx}
                    className="rounded text-center font-mono py-1"
                    style={row ? cellStyle(row.total_r, maxAbs) : undefined}
                    title={row ? cellTitle(row) : 'No trades'}
                  >
                    {row ? fmtSigned(row.total_r, 1) : ''}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
