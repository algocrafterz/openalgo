import type { StrategyDailyPerformance } from '@/api/strategyPnl'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { edgeVerdictText, pnlClass } from '@/lib/strategyPnlView'
import { cn } from '@/lib/utils'

function Stat({ label, value, className }: { label: string; value: string; className?: string }) {
  return (
    <div>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={cn('text-xl font-bold font-mono mt-1', className)}>{value}</div>
    </div>
  )
}

/** Is the average result per trade, after charges, more than luck? */
export function EdgeCheckCard({
  daily,
  formatCurrency,
}: {
  daily: StrategyDailyPerformance
  formatCurrency: (value: number) => string
}) {
  const n = daily.edge_trades ?? 0
  const ci = daily.expectancy_ci95
  const covered = daily.edge_cost_coverage ?? n
  return (
    <Card>
      <CardHeader>
        <CardTitle>Edge check</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid grid-cols-2 sm:grid-cols-5 gap-4">
          <Stat
            label="Net per trade"
            value={daily.net_expectancy == null ? '—' : formatCurrency(daily.net_expectancy)}
            className={pnlClass(daily.net_expectancy ?? 0)}
          />
          <Stat
            label="95% range"
            value={ci ? `${formatCurrency(ci[0])} to ${formatCurrency(ci[1])}` : '—'}
            className="text-base"
          />
          <Stat
            label="t-stat (trades)"
            value={daily.t_stat == null ? '—' : daily.t_stat.toFixed(2)}
          />
          <Stat
            label={`t-stat (${daily.edge_days ?? 0} days)`}
            value={daily.day_t_stat == null ? '—' : daily.day_t_stat.toFixed(2)}
          />
          <Stat label="Verdict" value={edgeVerdictText(daily.edge_verdict)} className="text-base" />
        </div>
        <p className="text-xs text-muted-foreground">
          A t-stat above 2 means the average is unlikely to be luck; below -2 means the average is
          unlikely to be a bad run. Based on {n} trades after estimated charges
          {covered < n ? ` (charges known for ${covered} of ${n})` : ''}. Trades on the same day
          move together, so the verdict also checks each trading day as one result: it needs at
          least 30 trades over 10 trading days, with both t-stats agreeing.
        </p>
      </CardContent>
    </Card>
  )
}
