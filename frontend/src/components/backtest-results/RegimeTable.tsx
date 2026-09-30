import type { RegimeRow, RegimesBlock } from '@/api/backtestResults'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { fmtNum, fmtPercent, signClass } from '@/lib/backtestResults'
import { cn } from '@/lib/utils'

function RegimeGroup({ title, rows }: { title: string; rows: RegimeRow[] }) {
  if (rows.length === 0) return null
  return (
    <div className="space-y-1">
      <div className="text-sm font-medium">{title}</div>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Regime</TableHead>
            <TableHead className="text-right">Trades</TableHead>
            <TableHead className="text-right">Net R per trade</TableHead>
            <TableHead className="text-right">Total R</TableHead>
            <TableHead className="text-right">Win rate</TableHead>
            <TableHead className="text-right">Max drawdown (R)</TableHead>
            <TableHead>Sample</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((r) => (
            <TableRow key={r.regime} data-testid={`regime-${title}-${r.regime}`}>
              <TableCell>{r.regime}</TableCell>
              <TableCell className="text-right font-mono">{r.trades}</TableCell>
              <TableCell className={cn('text-right font-mono', signClass(r.net_r))}>
                {fmtNum(r.net_r, 3)}
              </TableCell>
              <TableCell className="text-right font-mono">{fmtNum(r.total_r, 1)}</TableCell>
              <TableCell className="text-right font-mono">{fmtPercent(r.win_rate)}</TableCell>
              <TableCell className="text-right font-mono">{fmtNum(r.max_dd_r, 1)}</TableCell>
              <TableCell className="text-sm text-muted-foreground">
                {r.low_sample ? 'low sample: read with caution' : 'ok'}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

/**
 * Results split by the market regime of each trade's entry day. A regime that looks good
 * here is a hypothesis to test out-of-sample, not a filter to switch on.
 */
export function RegimeTable({ regimes }: { regimes: RegimesBlock }) {
  const unclassified = regimes.unclassified_trades ?? 0
  return (
    <Card data-testid="regimes">
      <CardHeader>
        <CardTitle>Performance by market regime</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <RegimeGroup title="Trend" rows={regimes.trend} />
        <RegimeGroup title="Volatility" rows={regimes.vol} />
        <p className="text-xs text-muted-foreground">
          Regimes come from data up to the prior day's close only (rule version {regimes.version}).
          Regimes with fewer than {regimes.min_trades ?? 30} trades are flagged, not hidden.
          {unclassified > 0 && ` ${unclassified} trades fell outside the classified history.`}
        </p>
      </CardContent>
    </Card>
  )
}
