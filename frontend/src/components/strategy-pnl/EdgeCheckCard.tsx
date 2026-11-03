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
  const ci90 = daily.expectancy_ci90
  const boot = daily.bootstrap_ci95
  const range = (v: [number, number] | null | undefined) =>
    v ? `${formatCurrency(v[0])} to ${formatCurrency(v[1])}` : '—'
  const covered = daily.edge_cost_coverage ?? n
  return (
    <Card>
      <CardHeader>
        <CardTitle>Edge check</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4">
          <Stat
            label="Net per trade"
            value={daily.net_expectancy == null ? '—' : formatCurrency(daily.net_expectancy)}
            className={pnlClass(daily.net_expectancy ?? 0)}
          />
          <Stat label="90% range" value={range(ci90)} className="text-base" />
          <Stat label="95% range" value={range(ci)} className="text-base" />
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
          unlikely to be a bad run. The ranges treat every trade as independent. Resampling whole
          trading days gives {range(boot)} at 95%
          {daily.bootstrap_p_nonpositive == null
            ? ''
            : `, and ${(daily.bootstrap_p_nonpositive * 100).toFixed(1)}% of resamples averaged zero or less`}
          . Based on {n} trades after estimated charges
          {covered < n ? ` (charges known for ${covered} of ${n})` : ''}. Trades on the same day
          move together, so the verdict also checks each trading day as one result: it needs at
          least 30 trades over 10 trading days, with both t-stats at 2 or above and the resampled
          95% range above zero.
        </p>
      </CardContent>
    </Card>
  )
}
