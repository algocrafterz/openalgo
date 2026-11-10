import type { DiagnosticGroup, RunRecord, TradeDiagnostics } from '@/api/backtestResults'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { fmtNum, fmtPercent, fmtSigned } from '@/lib/backtestResults'
import { cn } from '@/lib/utils'
import { RegimeTable } from './RegimeTable'

function Group({
  title,
  rows,
  testId,
}: {
  title: string
  rows: DiagnosticGroup[] | undefined
  testId: string
}) {
  return (
    <div className="space-y-1" data-testid={testId}>
      <div className="text-sm font-medium">{title}</div>
      {!rows || rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">Not available for this run.</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Group</TableHead>
              <TableHead className="text-right">Trades</TableHead>
              <TableHead className="text-right">Average net bps per trade</TableHead>
              <TableHead className="text-right">Total net bps</TableHead>
              <TableHead className="text-right">Win rate</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((r) => (
              <TableRow key={r.label}>
                <TableCell>{r.label}</TableCell>
                <TableCell className="text-right font-mono">{r.trades}</TableCell>
                <TableCell
                  className={cn(
                    'text-right font-mono',
                    r.net_bps_mean < 0 ? 'text-red-600' : 'text-green-600'
                  )}
                >
                  {fmtSigned(r.net_bps_mean, 1)}
                </TableCell>
                <TableCell className="text-right font-mono">{fmtNum(r.net_bps_total, 0)}</TableCell>
                <TableCell className="text-right font-mono">{fmtPercent(r.win_rate)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  )
}

/**
 * Where the result came from: by instrument, weekday, entry time, exit reason, side and market
 * regime. Each split is a plain grouping of the run's stored trades; none is a test.
 */
export function StrategyDiagnostics({
  run,
  diagnostics,
}: {
  run: RunRecord
  diagnostics: TradeDiagnostics | null | undefined
}) {
  return (
    <div className="space-y-6" data-testid="strategy-diagnostics">
      {!diagnostics && (
        <p className="text-sm text-muted-foreground" data-testid="diagnostics-unavailable">
          The trade list is not stored for this run, so the splits by instrument, weekday, entry
          time, exit reason and side are not available.
        </p>
      )}
      {diagnostics && (
        <>
          <p className="text-xs text-muted-foreground">
            Net result per trade in basis points of the position value (100 bps = 1%), after costs,
            over {diagnostics.trades} trades. Small groups are noisy.
          </p>
          <Group
            title={`Best instruments (of ${diagnostics.symbols_total ?? 'n/a'})`}
            rows={diagnostics.symbols_best}
            testId="diag-best"
          />
          <Group title="Worst instruments" rows={diagnostics.symbols_worst} testId="diag-worst" />
          <Group title="By weekday of entry" rows={diagnostics.by_weekday} testId="diag-weekday" />
          <Group
            title="By hour of entry (IST)"
            rows={diagnostics.by_entry_hour}
            testId="diag-hour"
          />
          <Group title="By exit reason" rows={diagnostics.by_exit_reason} testId="diag-exit" />
          <Group title="Long and short" rows={diagnostics.by_side} testId="diag-side" />
        </>
      )}
      {run.regimes ? (
        <RegimeTable regimes={run.regimes} />
      ) : (
        <p className="text-sm text-muted-foreground" data-testid="regimes-unavailable">
          Market regime split: not available for this run.
        </p>
      )}
    </div>
  )
}
