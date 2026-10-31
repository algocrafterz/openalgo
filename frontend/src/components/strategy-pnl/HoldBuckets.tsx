import type { HoldBucketPnl } from '@/api/strategyPnl'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { cn } from '@/lib/utils'

function pnlClass(value: number): string {
  return value > 0 ? 'text-green-600' : value < 0 ? 'text-red-600' : ''
}

export function HoldBuckets({
  rows,
  formatCurrency,
}: {
  rows: HoldBucketPnl[]
  formatCurrency: (v: number) => string
}) {
  if (rows.length === 0) return null
  const maxAbs = Math.max(...rows.map((r) => Math.abs(r.net_pnl)), 0)
  return (
    <Card>
      <CardHeader>
        <CardTitle>Result by holding time</CardTitle>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Held for</TableHead>
              <TableHead className="text-right">Trades</TableHead>
              <TableHead className="text-right">Win Rate</TableHead>
              <TableHead className="text-right">Avg P&amp;L / trade</TableHead>
              <TableHead className="text-right">Net P&amp;L</TableHead>
              <TableHead className="w-32 hidden sm:table-cell" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((r) => {
              const pct = maxAbs > 0 ? (Math.abs(r.net_pnl) / maxAbs) * 100 : 0
              return (
                <TableRow key={r.bucket}>
                  <TableCell className="font-medium">{r.bucket}</TableCell>
                  <TableCell className="text-right font-mono">{r.trades}</TableCell>
                  <TableCell className="text-right font-mono">{r.win_rate.toFixed(0)}%</TableCell>
                  <TableCell className={cn('text-right font-mono', pnlClass(r.avg_pnl))}>
                    {formatCurrency(r.avg_pnl)}
                  </TableCell>
                  <TableCell className={cn('text-right font-mono', pnlClass(r.net_pnl))}>
                    {formatCurrency(r.net_pnl)}
                  </TableCell>
                  <TableCell className="hidden sm:table-cell">
                    <div className="h-2 w-full rounded bg-muted">
                      <div
                        className={cn(
                          'h-2 rounded',
                          r.net_pnl >= 0 ? 'bg-green-600' : 'bg-red-600'
                        )}
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  )
}
