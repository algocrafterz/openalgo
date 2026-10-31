import type { EntryHourPnl, HourPnl, WeekdayPnl } from '@/api/strategyPnl'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { fmtDuration, hourLabel } from '@/lib/strategyPnlView'
import { cn } from '@/lib/utils'

interface Row {
  key: string
  label: string
  trades: number
  win_rate: number
  net_pnl: number
  avg_hold_min?: number | null
}

function pnlClass(value: number): string {
  return value > 0 ? 'text-green-600' : value < 0 ? 'text-red-600' : ''
}

function BreakdownTable({
  title,
  firstColumn,
  rows,
  formatCurrency,
  showHold = false,
}: {
  title: string
  firstColumn: string
  rows: Row[]
  formatCurrency: (v: number) => string
  showHold?: boolean
}) {
  return (
    <div>
      <div className="text-sm font-medium mb-2">{title}</div>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>{firstColumn}</TableHead>
            <TableHead className="text-right">Trades</TableHead>
            <TableHead className="text-right">Win Rate</TableHead>
            <TableHead className="text-right">Net P&amp;L</TableHead>
            {showHold && <TableHead className="text-right">Avg Hold</TableHead>}
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((r) => (
            <TableRow key={r.key}>
              <TableCell className="font-medium">{r.label}</TableCell>
              <TableCell className="text-right font-mono">{r.trades}</TableCell>
              <TableCell className="text-right font-mono">{r.win_rate.toFixed(0)}%</TableCell>
              <TableCell className={cn('text-right font-mono', pnlClass(r.net_pnl))}>
                {formatCurrency(r.net_pnl)}
              </TableCell>
              {showHold && (
                <TableCell className="text-right font-mono">
                  {fmtDuration(r.avg_hold_min)}
                </TableCell>
              )}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

export function CloseBreakdowns({
  byEntryHour,
  byHour,
  byWeekday,
  formatCurrency,
}: {
  byEntryHour: EntryHourPnl[]
  byHour: HourPnl[]
  byWeekday: WeekdayPnl[]
  formatCurrency: (v: number) => string
}) {
  if (byEntryHour.length === 0 && byHour.length === 0 && byWeekday.length === 0) return null
  const toRows = (list: HourPnl[]): Row[] =>
    list.map((h) => ({ ...h, key: String(h.hour), label: hourLabel(h.hour) }))
  const dayRows: Row[] = byWeekday.map((d) => ({ ...d, key: d.weekday, label: d.weekday }))
  return (
    <Card>
      <CardHeader>
        <CardTitle>Timing</CardTitle>
      </CardHeader>
      <CardContent>
        <p className="text-xs text-muted-foreground mb-4">
          Entry hour uses the time the position was opened; trades with no recorded entry time are
          left out.
        </p>
        <div className="grid gap-6 grid-cols-1 lg:grid-cols-2">
          {byEntryHour.length > 0 && (
            <div className="lg:col-span-2">
              <BreakdownTable
                title="By entry hour"
                firstColumn="Entry hour"
                rows={toRows(byEntryHour).map((r, i) => ({
                  ...r,
                  avg_hold_min: byEntryHour[i].avg_hold_min,
                }))}
                formatCurrency={formatCurrency}
                showHold
              />
            </div>
          )}
          {byHour.length > 0 && (
            <BreakdownTable
              title="By exit hour"
              firstColumn="Exit hour"
              rows={toRows(byHour)}
              formatCurrency={formatCurrency}
            />
          )}
          {dayRows.length > 0 && (
            <BreakdownTable
              title="By weekday"
              firstColumn="Weekday"
              rows={dayRows}
              formatCurrency={formatCurrency}
            />
          )}
        </div>
      </CardContent>
    </Card>
  )
}
