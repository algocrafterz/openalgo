import type { StrategyDailyPerformance } from '@/api/strategyPnl'
import { MetricCard } from '@/components/strategy-pnl/MetricCard'
import { chargesHint, fmtR, rCoverage, slippageHint } from '@/lib/strategyPnlView'

/** The summary cards for one trading session. */
export function SessionSummary({
  daily,
  formatCurrency,
}: {
  daily: StrategyDailyPerformance
  formatCurrency: (value: number) => string
}) {
  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
      <MetricCard
        label="Net P&L (gross)"
        value={formatCurrency(daily.net_profit ?? 0)}
        positive={(daily.net_profit ?? 0) > 0}
        negative={(daily.net_profit ?? 0) < 0}
      />
      <MetricCard
        label="After est. charges"
        value={daily.net_after_costs == null ? '—' : formatCurrency(daily.net_after_costs)}
        positive={(daily.net_after_costs ?? 0) > 0}
        negative={(daily.net_after_costs ?? 0) < 0}
        hint={chargesHint(daily, formatCurrency)}
      />
      <MetricCard
        label="Entry slippage"
        value={daily.entry_slippage == null ? '—' : formatCurrency(daily.entry_slippage)}
        hint={
          daily.entry_slippage_bps == null
            ? slippageHint(daily)
            : `${daily.entry_slippage_bps.toFixed(1)} bps, ${slippageHint(daily)}`
        }
      />
      <MetricCard
        label="Wins / Losses"
        value={`${daily.winning_trades ?? 0} / ${daily.losing_trades ?? 0}`}
        hint={`${daily.trades_count ?? 0} trades, ${(daily.win_rate ?? 0).toFixed(0)}% win`}
      />
      <MetricCard
        label="Total R"
        value={fmtR(daily.total_r)}
        positive={(daily.total_r ?? 0) > 0}
        negative={(daily.total_r ?? 0) < 0}
        hint={rCoverage(daily)}
      />
      <MetricCard
        label="Avg Win"
        value={formatCurrency(daily.avg_win ?? 0)}
        hint="per winning trade"
      />
      <MetricCard
        label="Avg Loss"
        value={formatCurrency(daily.avg_loss ?? 0)}
        hint="per losing trade"
      />
    </div>
  )
}
