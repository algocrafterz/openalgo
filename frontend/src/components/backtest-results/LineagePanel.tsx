import type { Lineage } from '@/api/backtestResults'
import { fmtNum, fmtSigned } from '@/lib/backtestResults'

const TREND_TEXT: Record<Lineage['trend'], string> = {
  improving: 'improving',
  stagnant: 'stagnant',
  regressing: 'regressing',
  insufficient: 'too few runs to judge',
}

/** How the idea has moved across its recorded runs, computed by the registry. */
export function LineagePanel({ lineage }: { lineage: Lineage }) {
  const d = lineage.delta_vs_parent
  return (
    <div className="rounded-md border p-3 text-sm space-y-1 max-w-3xl" data-testid="lineage">
      <div>
        Evolution: <span className="font-medium">{TREND_TEXT[lineage.trend]}</span> over{' '}
        {lineage.runs} run{lineage.runs === 1 ? '' : 's'}, {lineage.trials_sum} variant
        {lineage.trials_sum === 1 ? '' : 's'} tried in total.
      </div>
      {d && (
        <div className="text-muted-foreground">
          Latest run against its parent ({d.parent_run_id}): net {fmtSigned(d.net_bps)} bps
          {d.t_stat !== undefined && `, t ${fmtSigned(d.t_stat)}`}, {fmtNum(d.trades, 0)} trades
          difference.
        </div>
      )}
    </div>
  )
}
