import type { CostComponentSummary } from '@/api/backtestResults'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { fmtNum } from '@/lib/backtestResults'
import { InfoTip } from './InfoTip'

function Components({ cc }: { cc: CostComponentSummary }) {
  const components = cc.components ?? []
  const total = components.reduce((a, c) => a + c.mean_bps, 0)
  return (
    <div className="space-y-2">
      <Table data-testid="cost-components">
        <TableHeader>
          <TableRow>
            <TableHead>Component</TableHead>
            <TableHead className="text-right">Mean bps</TableHead>
            <TableHead className="text-right">Share of total</TableHead>
            <TableHead className="w-40">
              <span className="sr-only">Bar</span>
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {components.map((c) => {
            const share = total > 0 ? (c.mean_bps / total) * 100 : null
            return (
              <TableRow key={c.key}>
                <TableCell>{c.label}</TableCell>
                <TableCell className="text-right font-mono">{fmtNum(c.mean_bps)}</TableCell>
                <TableCell className="text-right font-mono">
                  {share === null ? 'n/a' : `${fmtNum(share, 1)}%`}
                </TableCell>
                <TableCell>
                  <div className="h-2 rounded bg-muted" aria-hidden="true">
                    <div
                      className="h-2 rounded bg-foreground/60"
                      style={{ width: `${share ?? 0}%` }}
                    />
                  </div>
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
      <dl className="flex flex-wrap gap-6 text-sm">
        <div>
          <dt className="text-muted-foreground">Engine flat cost (mean bps per trade)</dt>
          <dd className="font-mono" data-testid="cost-engine">
            {fmtNum(cc.engine_cost_bps_mean)}
          </dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Modelled cost (mean bps per trade)</dt>
          <dd className="font-mono" data-testid="cost-modeled">
            {fmtNum(cc.modeled_cost_bps_mean)}
          </dd>
        </div>
      </dl>
      <p data-testid="cost-note" className="text-xs text-muted-foreground">
        The headline net result uses the engine's flat cost; these modelled components show where a
        realistic cost comes from.
      </p>
    </div>
  )
}

/** Before-cost and after-cost headline figures, and the modelled per-trade cost components. */
export function CostDecomposition({
  costComponents,
  grossBps,
  netBps,
}: {
  costComponents: CostComponentSummary | null
  grossBps: number | null | undefined
  netBps: number | null | undefined
}) {
  return (
    <Card data-testid="cost-decomposition" className="max-w-3xl">
      <CardHeader>
        <CardTitle className="text-base">
          Before and after costs <InfoTip term="cost_components" />
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <dl className="grid grid-cols-2 gap-4 text-sm max-w-md">
          <div className="rounded border p-2">
            <dt className="text-muted-foreground">Before costs (gross bps per trade)</dt>
            <dd className="font-mono text-lg" data-testid="cost-before">
              {fmtNum(grossBps)}
            </dd>
          </div>
          <div className="rounded border p-2">
            <dt className="text-muted-foreground">After costs (net bps per trade)</dt>
            <dd className="font-mono text-lg" data-testid="cost-after">
              {fmtNum(netBps)}
            </dd>
          </div>
        </dl>
        {costComponents ? (
          <Components cc={costComponents} />
        ) : (
          <p className="text-sm text-muted-foreground">Cost components were not recorded</p>
        )}
      </CardContent>
    </Card>
  )
}
