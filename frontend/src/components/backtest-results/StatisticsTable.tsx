import type { PortfolioView, RunRecord } from '@/api/backtestResults'
import { fmtNum, fmtPercent, fmtRupees, fmtSigned, isKnown, rupeePnl } from '@/lib/backtestResults'
import { cn } from '@/lib/utils'

interface StatRow {
  key: string
  label: string
  value: string
  /** Positive or negative figures are coloured; everything else stays plain. */
  sign?: number | null
  hint?: string
}

interface StatGroup {
  title: string
  rows: StatRow[]
}

const monthly = (run: RunRecord, portfolio?: PortfolioView | null) => {
  const book = portfolio && !portfolio.unavailable ? portfolio.monthly_pct : undefined
  if (book?.length) return { values: book.map((m) => m.return_pct), unit: '%' }
  const r = run.series?.monthly
  if (r?.length) return { values: r.map((m) => m.total_r), unit: 'R' }
  return null
}

/**
 * The figures a trader looks for first, grouped as Performance, Trades and Risk. Every value is
 * read from the run as recorded (the best and worst month are the extremes of the recorded
 * months). A figure the run does not carry is left out, never shown as zero.
 */
export function statisticGroups(run: RunRecord, portfolio?: PortfolioView | null): StatGroup[] {
  const m = run.metrics
  const stats = portfolio && !portfolio.unavailable ? portfolio.stats : undefined
  const rs = rupeePnl(run)
  const gross = rs && rs.total_cost_rs !== null ? rs.net_rs + rs.total_cost_rs : null
  const months = monthly(run, portfolio)
  const best = months ? Math.max(...months.values) : null
  const worst = months ? Math.min(...months.values) : null

  const performance: StatRow[] = []
  if (rs) {
    if (gross !== null) {
      performance.push({
        key: 'gross',
        label: 'Gross P&L before costs',
        value: fmtRupees(gross, true),
        sign: gross,
      })
    }
    if (rs.total_cost_rs !== null) {
      performance.push({
        key: 'costs',
        label: 'Costs paid (brokerage, STT, charges, slippage)',
        value: fmtRupees(rs.total_cost_rs),
      })
    }
    performance.push({
      key: 'net',
      label: 'Net P&L after costs',
      value: fmtRupees(rs.net_rs, true),
      sign: rs.net_rs,
    })
  }

  const trades: StatRow[] = []
  if (isKnown(m, m.win_rate)) {
    trades.push({ key: 'win-rate', label: 'Win rate', value: fmtPercent(m.win_rate) })
  }
  if (isKnown(m, m.payoff)) {
    trades.push({ key: 'payoff', label: 'Average win / average loss', value: fmtNum(m.payoff) })
  }
  if (months && best !== null && worst !== null) {
    trades.push({
      key: 'months',
      label: 'Best / worst month',
      value: `${fmtSigned(best, 1)}${months.unit} / ${fmtSigned(worst, 1)}${months.unit}`,
    })
  }

  const risk: StatRow[] = []
  if (typeof stats?.max_drawdown_pct === 'number') {
    risk.push({
      key: 'drawdown',
      label: 'Max drawdown',
      value: `${fmtNum(stats.max_drawdown_pct, 1)}%`,
      sign: stats.max_drawdown_pct,
      hint:
        stats.max_drawdown_peak && stats.max_drawdown_trough
          ? `${stats.max_drawdown_peak} to ${stats.max_drawdown_trough}`
          : undefined,
    })
  } else if (isKnown(m, m.max_dd_r)) {
    risk.push({
      key: 'drawdown',
      label: 'Max drawdown',
      value: `${fmtNum(m.max_dd_r, 1)}R`,
      hint: 'in R: no portfolio book for this run',
    })
  }
  const sharpe = typeof stats?.sharpe === 'number' ? stats.sharpe : m.sharpe
  if (typeof sharpe === 'number' && Number.isFinite(sharpe)) {
    risk.push({
      key: 'sharpe',
      label: stats?.sharpe != null ? 'Sharpe ratio (daily book)' : 'Sharpe ratio',
      value: fmtNum(sharpe),
    })
  }

  return [
    { title: 'Performance', rows: performance },
    { title: 'Trades', rows: trades },
    { title: 'Risk', rows: risk },
  ].filter((g) => g.rows.length > 0)
}

/**
 * The significance figures, kept out of the first screen and shown with the validation details:
 * the t-statistic against the level it must reach, and the expectancy in R.
 */
export function SignificanceRows({ run }: { run: RunRecord }) {
  const m = run.metrics
  const rows: StatRow[] = []
  if (isKnown(m, m.t_stat)) {
    rows.push({
      key: 't',
      label: 'Significance (t-statistic, against the level it must reach)',
      value: isKnown(m, m.hurdle_t)
        ? `${fmtNum(m.t_stat)} vs ${fmtNum(m.hurdle_t)}`
        : fmtNum(m.t_stat),
    })
  }
  if (isKnown(m, m.expectancy_r)) {
    rows.push({
      key: 'expectancy',
      label: 'Expectancy per trade (R)',
      value: fmtSigned(m.expectancy_r, 2),
      sign: m.expectancy_r,
    })
  }
  if (rows.length === 0) return null
  return (
    <table className="w-full text-sm" data-testid="significance-rows">
      <caption className="sr-only">Significance</caption>
      <tbody>
        {rows.map((r) => (
          <tr key={r.key} className="border-b last:border-0" data-testid={`stat-${r.key}`}>
            <th scope="row" className="py-1.5 text-left font-normal">
              {r.label}
            </th>
            <td className="py-1.5 text-right font-mono">{r.value}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** One plain table, in the style of a trading-journal report: a label and a value per row. */
export function StatisticsTable({
  run,
  portfolio,
}: {
  run: RunRecord
  portfolio?: PortfolioView | null
}) {
  const groups = statisticGroups(run, portfolio)
  if (groups.length === 0) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="statistics-empty">
        No summary statistics were recorded for this run.
      </p>
    )
  }
  return (
    <table className="w-full text-sm" data-testid="statistics-table">
      <caption className="sr-only">Summary statistics</caption>
      {groups.map((g) => (
        <tbody key={g.title}>
          <tr>
            <th
              colSpan={2}
              scope="colgroup"
              className="pb-1 pt-5 text-left text-[11px] font-semibold uppercase tracking-wide text-muted-foreground [tbody:first-of-type_&]:pt-0"
            >
              {g.title}
            </th>
          </tr>
          {g.rows.map((r) => (
            <tr key={r.key} className="border-b last:border-0" data-testid={`stat-${r.key}`}>
              <th scope="row" className="py-1.5 text-left font-normal">
                {r.label}
                {r.hint && <span className="block text-xs text-muted-foreground">{r.hint}</span>}
              </th>
              <td
                className={cn(
                  'py-1.5 text-right font-mono',
                  r.sign != null && r.sign > 0 && 'text-green-600',
                  r.sign != null && r.sign < 0 && 'text-red-600'
                )}
              >
                {r.value}
              </td>
            </tr>
          ))}
        </tbody>
      ))}
    </table>
  )
}
