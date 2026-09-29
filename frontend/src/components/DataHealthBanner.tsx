import { AlertTriangle, Info } from 'lucide-react'
import type { DataHealth } from '@/api/strategyPnl'
import { Alert } from '@/components/ui/alert'

function formatIst(iso: string | null): string | null {
  if (!iso) return null
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return null
  return `${date.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false })} IST`
}

interface DataHealthBannerProps {
  health?: DataHealth
}

/**
 * Warns when the strategy book could not be confirmed against the broker, so
 * P&L / performance figures may be missing fills (e.g. after a crash or restart,
 * or while the order-update feed was down). Renders nothing when data is healthy.
 */
export function DataHealthBanner({ health }: DataHealthBannerProps) {
  if (!health) return null

  const checkedAt = formatIst(health.last_reconciled_at)
  const recovered =
    health.recovered_fills > 0
      ? `Recovered ${health.recovered_fills} missed fill${health.recovered_fills === 1 ? '' : 's'} from the broker order book since the app started.`
      : null

  if (health.status === 'ok') {
    return recovered ? (
      <Alert>
        <Info className="h-4 w-4" />
        <div className="text-sm">
          {recovered} Figures were re-verified against the broker
          {checkedAt ? ` at ${checkedAt}` : ''}.
        </div>
      </Alert>
    ) : null
  }

  return (
    <Alert variant="warning" data-testid="data-health-warning">
      <AlertTriangle className="h-4 w-4" />
      <div className="space-y-1 text-sm">
        <p className="font-medium">
          {health.status === 'stale'
            ? 'These figures may be stale or incomplete'
            : 'Verifying figures'}
        </p>
        {health.warnings.map((warning) => (
          <p key={warning}>{warning}</p>
        ))}
        {recovered && <p>{recovered}</p>}
        {checkedAt && <p className="opacity-80">Last checked against the broker at {checkedAt}.</p>}
      </div>
    </Alert>
  )
}
