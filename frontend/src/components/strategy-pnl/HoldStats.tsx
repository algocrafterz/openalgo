import type { HoldStats } from '@/api/strategyPnl'
import { Card, CardContent } from '@/components/ui/card'
import { fmtDuration } from '@/lib/strategyPnlView'

function Tile({ label, value }: { label: string; value: string }) {
  return (
    <Card>
      <CardContent className="py-4">
        <div className="text-xs text-muted-foreground">{label}</div>
        <div className="text-xl font-bold font-mono mt-1">{value}</div>
      </CardContent>
    </Card>
  )
}

const EXCLUDED_TEXT: Record<string, string> = {
  overnight_mis: 'intraday position recorded as held overnight',
  missing_entry_time: 'no entry time recorded',
}

function excludedText(excluded: Record<string, number> | undefined): string {
  const parts = Object.entries(excluded ?? {})
    .filter(([, n]) => n > 0)
    .map(([reason, n]) => `${n} ${EXCLUDED_TEXT[reason] ?? reason}`)
  return parts.length ? ` (left out: ${parts.join(', ')})` : ''
}

export function HoldStatsRow({ stats }: { stats: HoldStats }) {
  if (stats.covered === 0) return null
  return (
    <div>
      <div className="text-sm font-medium mb-2">Holding time</div>
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
        <Tile label="Avg hold" value={fmtDuration(stats.avg_min)} />
        <Tile label="Median hold" value={fmtDuration(stats.median_min)} />
        <Tile label="90% of trades closed within" value={fmtDuration(stats.p90_min)} />
        <Tile label="Winners avg hold" value={fmtDuration(stats.winners_avg_min)} />
        <Tile label="Losers avg hold" value={fmtDuration(stats.losers_avg_min)} />
      </div>
      {stats.covered < stats.total && (
        <p className="text-xs text-muted-foreground mt-2">
          Holding time shown for {stats.covered} of {stats.total} trades
          {excludedText(stats.excluded)}. Their profit and loss is still counted everywhere else.
        </p>
      )}
    </div>
  )
}
