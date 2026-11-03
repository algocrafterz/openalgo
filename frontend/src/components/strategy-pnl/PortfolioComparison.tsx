import type { StrategyComparisonResponse } from '@/api/strategyPnl'
import { GradeBadge } from '@/components/strategy-pnl/InsightsPanel'
import { ValidationBadge } from '@/components/strategy-pnl/ValidationStatusCard'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { pnlClass } from '@/lib/strategyPnlView'
import { cn } from '@/lib/utils'

const dash = '—'

/** Does combining the strategies help? Combined results, correlation and drawdown with and without each. */
export function PortfolioComparison({
  data,
  formatCurrency: money,
}: {
  data: StrategyComparisonResponse
  formatCurrency: (value: number) => string
}) {
  const p = data.portfolio
  const corr = data.correlation
  const contribution = data.contribution ?? []
  if (!p) return null
  return (
    <div className="space-y-4 py-4 border-t mt-2">
      <div className="text-sm font-medium">Combined portfolio</div>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Net P&amp;L</TableHead>
            <TableHead className="text-right">Net PF</TableHead>
            <TableHead className="text-right">Return on capital</TableHead>
            <TableHead className="text-right">Max drawdown (net)</TableHead>
            <TableHead className="text-right">Recovery</TableHead>
            <TableHead className="text-right">Grade</TableHead>
            <TableHead className="text-right">Validation</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          <TableRow>
            <TableCell className={cn('font-mono', pnlClass(p.net))}>{money(p.net)}</TableCell>
            <TableCell className="text-right font-mono">
              {p.net_profit_factor?.toFixed(2) ?? dash}
            </TableCell>
            <TableCell className="text-right font-mono">
              {p.return_on_capital_pct.toFixed(2)}%
            </TableCell>
            <TableCell className="text-right font-mono">
              {money(Math.abs(p.max_drawdown))} ({p.max_drawdown_pct.toFixed(2)}%)
            </TableCell>
            <TableCell className="text-right font-mono">
              {p.recovery_factor?.toFixed(2) ?? dash}
            </TableCell>
            <TableCell className="text-right" title={p.overall_note ?? undefined}>
              <GradeBadge grade={p.overall_grade} preliminary={p.preliminary_grade} />
            </TableCell>
            <TableCell className="text-right">
              <ValidationBadge
                state={p.validation_status}
                label={p.validation_label}
                title={p.validation_summary}
              />
            </TableCell>
          </TableRow>
        </TableBody>
      </Table>
      {contribution.length > 1 && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Strategy</TableHead>
              <TableHead className="text-right">Net P&amp;L</TableHead>
              <TableHead className="text-right">Share of profit</TableHead>
              <TableHead className="text-right">Portfolio drawdown with it</TableHead>
              <TableHead className="text-right">Portfolio drawdown without it</TableHead>
              <TableHead className="text-right">Effect on drawdown</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {contribution.map((c) => (
              <TableRow key={c.strategy}>
                <TableCell>{c.strategy}</TableCell>
                <TableCell className={cn('text-right font-mono', pnlClass(c.net))}>
                  {money(c.net)}
                </TableCell>
                <TableCell className="text-right font-mono">
                  {c.share_of_net_pct == null ? dash : `${c.share_of_net_pct.toFixed(1)}%`}
                </TableCell>
                <TableCell className="text-right font-mono">
                  {money(Math.abs(c.portfolio_dd_with))}
                </TableCell>
                <TableCell className="text-right font-mono">
                  {money(Math.abs(c.portfolio_dd_without))}
                </TableCell>
                <TableCell className="text-right text-xs">
                  {c.drawdown_change > 0
                    ? `Smaller by ${money(c.drawdown_change)}`
                    : c.drawdown_change < 0
                      ? `Larger by ${money(Math.abs(c.drawdown_change))}`
                      : 'No change'}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {corr && corr.strategies.length > 1 && (
        <div className="space-y-1">
          <div className="text-sm font-medium">Correlation of daily results</div>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead />
                {corr.strategies.map((n) => (
                  <TableHead key={n} className="text-right">
                    {n}
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {corr.strategies.map((n, i) => (
                <TableRow key={n}>
                  <TableCell>{n}</TableCell>
                  {corr.matrix[i].map((v, j) => (
                    <TableCell key={corr.strategies[j]} className="text-right font-mono">
                      {v === null ? dash : v.toFixed(2)}
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      <p className="text-xs text-muted-foreground">
        Everything here is after estimated charges. Correlation needs at least 20 trading sessions,
        and with few sessions it moves a lot; a low number now does not show the strategies will
        keep hedging each other. Return on capital uses the reference capital the recorded trades
        were sized on.
      </p>
    </div>
  )
}
