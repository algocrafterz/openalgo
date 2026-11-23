import type {
  CalendarRow,
  DiscretionaryCategory,
  PointsByInstrumentRow,
} from '@/api/discretionaryPnl'
import { CATEGORY_CHOICES } from '@/api/discretionaryPnl'
import { Button } from '@/components/ui/button'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { fmtPoints, fmtWinRate, pnlTextClass } from '@/lib/discretionaryPnlView'
import { cn } from '@/lib/utils'

type Money = (value: number) => string

/** All trades / Intraday / Swing. Unclassified positions are part of All trades only. */
export function CategorySelector({
  value,
  onChange,
}: {
  value: DiscretionaryCategory
  onChange: (next: DiscretionaryCategory) => void
}) {
  return (
    <fieldset aria-label="Trade category" className="m-0 inline-flex gap-1 border-0 p-0">
      {CATEGORY_CHOICES.map((c) => (
        <Button
          key={c.value}
          type="button"
          size="sm"
          variant={value === c.value ? 'default' : 'outline'}
          aria-pressed={value === c.value}
          onClick={() => onChange(c.value)}
        >
          {c.label}
        </Button>
      ))}
    </fieldset>
  )
}

/** Wins and losses below this many positions are indicative only (the edge-check minimum). */

/**
 * Points per unit for each instrument. Points are never added across instruments, so there is
 * no total row; symbol and exchange are always shown.
 */
export function PointsByInstrumentTable({ rows }: { rows: PointsByInstrumentRow[] }) {
  if (rows.length === 0) {
    return <p className="text-sm text-muted-foreground">No completed trades in this range.</p>
  }
  return (
    <div className="min-w-0 overflow-x-auto" data-testid="points-by-instrument">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Symbol</TableHead>
            <TableHead>Exchange</TableHead>
            <TableHead className="text-right">Positions</TableHead>
            <TableHead className="text-right">Wins / Losses</TableHead>
            <TableHead className="text-right">Points gained</TableHead>
            <TableHead className="text-right">Points lost</TableHead>
            <TableHead className="text-right">Net points</TableHead>
            <TableHead className="text-right">Avg per position</TableHead>
            <TableHead className="text-right">Avg win</TableHead>
            <TableHead className="text-right">Avg loss</TableHead>
            <TableHead className="text-right">Largest win</TableHead>
            <TableHead className="text-right">Largest loss</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((r) => (
            <TableRow key={`${r.symbol}-${r.exchange}`} data-testid="points-row">
              <TableCell className="font-medium">{r.symbol}</TableCell>
              <TableCell>{r.exchange}</TableCell>
              <TableCell className="text-right font-mono">{r.positions}</TableCell>
              <TableCell className="text-right font-mono">
                {r.winners} / {r.losers}
              </TableCell>
              <TableCell
                className={cn(
                  'text-right font-mono',
                  r.gross_points_gained > 0
                    ? 'text-green-700 dark:text-green-600 [.analyzer_&]:text-green-600'
                    : 'text-muted-foreground'
                )}
              >
                {fmtPoints(r.gross_points_gained)}
              </TableCell>
              <TableCell
                className={cn(
                  'text-right font-mono',
                  r.gross_points_lost > 0
                    ? 'text-red-600 dark:text-red-400'
                    : 'text-muted-foreground'
                )}
              >
                {r.gross_points_lost > 0 ? `-${r.gross_points_lost.toFixed(2)}` : '0.00'}
              </TableCell>
              <TableCell className={cn('text-right font-mono', pnlTextClass(r.net_points))}>
                {fmtPoints(r.net_points)}
              </TableCell>
              <TableCell
                className={cn('text-right font-mono', pnlTextClass(r.avg_points_per_position ?? 0))}
              >
                {fmtPoints(r.avg_points_per_position)}
              </TableCell>
              <TableCell className="text-right font-mono">{fmtPoints(r.avg_win_points)}</TableCell>
              <TableCell className="text-right font-mono">{fmtPoints(r.avg_loss_points)}</TableCell>
              <TableCell className="text-right font-mono">
                {fmtPoints(r.largest_win_points)}
              </TableCell>
              <TableCell className="text-right font-mono">
                {fmtPoints(r.largest_loss_points)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

/** Monthly or weekday rows of the closed positions, by the date of the final close. */
export function CalendarTable({
  rows,
  first,
  label,
  formatCurrency,
}: {
  rows: CalendarRow[]
  first: string
  label: (row: CalendarRow) => string
  formatCurrency: Money
}) {
  return (
    <div className="min-w-0 overflow-x-auto">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>{first}</TableHead>
            <TableHead className="text-right">Positions</TableHead>
            <TableHead className="text-right">Win rate</TableHead>
            <TableHead className="text-right">Gross P&amp;L</TableHead>
            <TableHead className="text-right">Est. charges</TableHead>
            <TableHead className="text-right">Net P&amp;L</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((r) => (
            <TableRow key={r.key} data-testid="calendar-row">
              <TableCell className="font-medium">{label(r)}</TableCell>
              <TableCell className="text-right font-mono">{r.positions}</TableCell>
              <TableCell className="text-right font-mono">
                {r.win_rate == null ? '—' : fmtWinRate(r.win_rate)}
              </TableCell>
              <TableCell
                className={cn(
                  'text-right font-mono',
                  r.positions === 0 ? undefined : pnlTextClass(r.realized_gross)
                )}
              >
                {r.positions === 0 ? '—' : formatCurrency(r.realized_gross)}
              </TableCell>
              <TableCell className="text-right font-mono text-muted-foreground">
                {r.positions === 0 ? '—' : formatCurrency(r.charges)}
                {r.charges_unavailable_positions > 0 && (
                  <span
                    className="ml-0.5 text-xs text-amber-700 dark:text-amber-500 [.analyzer_&]:text-amber-500"
                    title={`${r.charges_unavailable_positions} position(s) have no charge estimate, so charges and net are not complete`}
                  >
                    *<span className="sr-only"> charges incomplete</span>
                  </span>
                )}
              </TableCell>
              <TableCell className={cn('text-right font-mono', pnlTextClass(r.net_of_charges))}>
                {r.positions === 0 ? '—' : formatCurrency(r.net_of_charges)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}
