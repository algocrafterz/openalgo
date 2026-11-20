import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, ChevronDown } from 'lucide-react'
import { useState } from 'react'
import {
  type DiscretionaryFilters,
  discretionaryPnlApi,
  type UnresolvedFill,
} from '@/api/discretionaryPnl'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import {
  DISCRETIONARY_POLL_MS,
  fmtPrice,
  fmtQty,
  fmtStamp,
  reasonText,
} from '@/lib/discretionaryPnlView'
import { cn } from '@/lib/utils'

function FillRows({ title, rows }: { title: string; rows: UnresolvedFill[] }) {
  if (rows.length === 0) return null
  return (
    <div>
      <div className="text-sm font-medium mb-2">
        {title} ({rows.length})
      </div>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Time</TableHead>
            <TableHead>Broker</TableHead>
            <TableHead>Account</TableHead>
            <TableHead>Symbol</TableHead>
            <TableHead>Product</TableHead>
            <TableHead>Side</TableHead>
            <TableHead className="text-right">Qty</TableHead>
            <TableHead className="text-right">Price</TableHead>
            <TableHead>Order</TableHead>
            <TableHead>Why</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row, i) => (
            <TableRow key={`${row.id ?? row.fill_ref ?? 'row'}-${i}`}>
              <TableCell className="font-mono">{fmtStamp(row.fill_time)}</TableCell>
              <TableCell>{row.broker || '—'}</TableCell>
              <TableCell>{row.account || '—'}</TableCell>
              <TableCell className="font-medium">{row.symbol || '—'}</TableCell>
              <TableCell>{row.product || '—'}</TableCell>
              <TableCell>{row.side || '—'}</TableCell>
              <TableCell className="text-right font-mono">{fmtQty(row.qty)}</TableCell>
              <TableCell className="text-right font-mono">{fmtPrice(row.price)}</TableCell>
              <TableCell className="font-mono text-xs">{row.order_id || '—'}</TableCell>
              <TableCell className="text-xs">
                {reasonText(row.reason ?? row.attribution_state, row.coverage_from)}
                {row.source_evidence && (
                  <div className="text-muted-foreground" data-testid="unresolved-evidence">
                    Source evidence: {row.source_evidence}
                  </div>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

/** Fills the ledger could not classify or place. They are listed here and never counted in P&L. */
export function UnresolvedSection({
  filters,
  defaultOpen = false,
}: {
  filters: DiscretionaryFilters
  /** Start expanded (the page does so when every execution of the selection awaits attribution). */
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  const query = useQuery({
    queryKey: ['discretionary-pnl', 'unresolved', filters],
    queryFn: () => discretionaryPnlApi.unresolved(filters),
    refetchInterval: DISCRETIONARY_POLL_MS,
    retry: false,
  })
  const unresolved = query.data?.unresolved ?? []
  const orphans = query.data?.orphans ?? []
  const count = (query.data?.unresolved_count ?? 0) + (query.data?.orphans_count ?? 0)

  return (
    <Card id="discretionary-unresolved" data-testid="discretionary-unresolved">
      <Collapsible open={open} onOpenChange={setOpen}>
        <CollapsibleTrigger
          className="flex w-full items-center justify-between px-6 py-4 text-left"
          aria-label="Unresolved fills"
        >
          <span className="font-semibold">
            Unresolved fills{' '}
            {query.isError ? (
              <span
                className="inline-flex items-center gap-1 text-red-600"
                data-testid="unresolved-error-indicator"
              >
                <AlertTriangle className="h-4 w-4" aria-hidden="true" />
                <span className="text-xs font-normal">could not load</span>
              </span>
            ) : (
              <span className={cn('font-mono', count > 0 && 'text-amber-600')}>
                {query.isLoading ? '' : `(${count})`}
              </span>
            )}
          </span>
          <ChevronDown className={cn('h-4 w-4 transition-transform', open && 'rotate-180')} />
        </CollapsibleTrigger>
        <CollapsibleContent>
          <CardContent className="space-y-4 pt-0">
            <p className="text-sm text-muted-foreground">
              Executions that could not be reliably classified are excluded from P&amp;L. They stay
              excluded until they are attributed.
            </p>
            {query.error ? (
              <Alert variant="destructive">
                <AlertDescription>
                  <p>{(query.error as Error).message}</p>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    className="mt-2"
                    onClick={() => query.refetch()}
                  >
                    Retry
                  </Button>
                </AlertDescription>
              </Alert>
            ) : count === 0 ? (
              <p className="text-sm text-muted-foreground">No unresolved fills.</p>
            ) : (
              <>
                <FillRows title="Unresolved" rows={unresolved} />
                <FillRows title="Could not be placed" rows={orphans} />
              </>
            )}
          </CardContent>
        </CollapsibleContent>
      </Collapsible>
    </Card>
  )
}
