import type { PerformanceTrade } from '@/api/strategyPnl'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { fmtDate, fmtDuration, fmtR, pnlClass, todayIst } from '@/lib/strategyPnlView'
import { cn } from '@/lib/utils'

function fmtTime(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? '—'
    : d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false })
}

const MFE_HINT = 'Best open profit during the trade (upper estimate from 1-minute bars)'
const MAE_HINT = 'Worst open loss during the trade (upper estimate from 1-minute bars)'

export function SessionTrades({
  trades,
  sessionDate,
  excursionCovered,
  excursionTotal,
  formatCurrency,
}: {
  trades: PerformanceTrade[]
  sessionDate: string | undefined
  excursionCovered: number
  excursionTotal: number
  formatCurrency: (value: number) => string
}) {
  const title =
    sessionDate === todayIst() ? "Today's Closed Trades" : `Closed Trades - ${fmtDate(sessionDate)}`
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent className="py-0">
        {excursionTotal > 0 && (
          <p className="text-xs text-muted-foreground pb-2">
            MFE/MAE for {excursionCovered} of {excursionTotal} trades
          </p>
        )}
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Entry</TableHead>
              <TableHead>Exit</TableHead>
              <TableHead>Held</TableHead>
              <TableHead>Strategy</TableHead>
              <TableHead>Symbol</TableHead>
              <TableHead>Side</TableHead>
              <TableHead className="text-right">Qty</TableHead>
              <TableHead className="text-right">Entry</TableHead>
              <TableHead className="text-right">Exit</TableHead>
              <TableHead className="text-right">P&amp;L</TableHead>
              <TableHead className="text-right">R</TableHead>
              <TableHead className="text-right" title={MFE_HINT}>
                MFE
              </TableHead>
              <TableHead className="text-right" title={MAE_HINT}>
                MAE
              </TableHead>
              <TableHead>Broker</TableHead>
              <TableHead className="text-right">Est. Charges</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {trades.map((t, i) => (
              <TableRow key={`${t.closed_at}-${t.symbol}-${t.strategy}-${i}`}>
                <TableCell className="font-mono">{fmtTime(t.opened_at ?? null)}</TableCell>
                <TableCell className="font-mono">{fmtTime(t.closed_at)}</TableCell>
                <TableCell className="font-mono">{fmtDuration(t.hold_min)}</TableCell>
                <TableCell>{t.strategy}</TableCell>
                <TableCell className="font-medium">{t.symbol}</TableCell>
                <TableCell>{t.direction}</TableCell>
                <TableCell className="text-right font-mono">{t.closed_quantity}</TableCell>
                <TableCell className="text-right font-mono">{t.entry_price.toFixed(2)}</TableCell>
                <TableCell className="text-right font-mono">{t.exit_price.toFixed(2)}</TableCell>
                <TableCell className={cn('text-right font-mono', pnlClass(t.realized_pnl))}>
                  {formatCurrency(t.realized_pnl)}
                </TableCell>
                <TableCell className={cn('text-right font-mono', pnlClass(t.r_multiple ?? 0))}>
                  {fmtR(t.r_multiple)}
                </TableCell>
                <TableCell className="text-right font-mono text-green-600">
                  {t.mfe == null ? '—' : formatCurrency(t.mfe)}
                </TableCell>
                <TableCell className="text-right font-mono text-red-600">
                  {t.mae == null ? '—' : formatCurrency(t.mae)}
                </TableCell>
                <TableCell className="text-muted-foreground">{t.broker ?? '—'}</TableCell>
                <TableCell className="text-right font-mono text-muted-foreground">
                  {t.cost === null ? '—' : formatCurrency(t.cost)}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  )
}
