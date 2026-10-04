import type { LedgerSummary } from '@/api/backtestResults'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { sortedExclusions } from '@/lib/backtestEvidence'
import { fmtNum } from '@/lib/backtestResults'
import { InfoTip } from './InfoTip'

const COUNTS: (keyof LedgerSummary)[] = [
  'signals',
  'accepted',
  'orders',
  'filled',
  'dropped',
  'fills',
  'trades',
]

/** Signal to trade counts, whether the ledgers reconcile, and why signals were excluded. */
export function LedgerSummaryBlock({ ledgers }: { ledgers: LedgerSummary | null }) {
  const exclusions = sortedExclusions(ledgers?.excluded_by_reason)
  const problems = ledgers?.problems ?? []
  const recorded = typeof ledgers?.reconcile_problems === 'number'
  const reconciles = recorded && ledgers?.reconcile_problems === 0 && problems.length === 0
  return (
    <Card data-testid="ledger-summary" className="max-w-3xl">
      <CardHeader>
        <CardTitle className="text-base">
          Lineage ledgers <InfoTip term="lineage" />
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {!ledgers ? (
          <p className="text-sm text-muted-foreground">No lineage ledgers for this run</p>
        ) : (
          <>
            <dl className="flex flex-wrap gap-5 text-sm">
              {COUNTS.map((k) => (
                <div key={k}>
                  <dt className="text-muted-foreground capitalize">{k}</dt>
                  <dd className="font-mono" data-testid={`ledger-${k}`}>
                    {fmtNum(typeof ledgers[k] === 'number' ? (ledgers[k] as number) : null, 0)}
                  </dd>
                </div>
              ))}
            </dl>
            <div data-testid="ledger-reconcile" className="text-sm">
              <span className="font-medium">
                Reconciliation <InfoTip term="reconciliation" />:{' '}
              </span>
              {!recorded ? (
                'Reconciliation not recorded'
              ) : reconciles ? (
                'Reconciles exactly'
              ) : (
                <>
                  {ledgers.reconcile_problems} problem{ledgers.reconcile_problems === 1 ? '' : 's'}
                  <ul className="list-disc pl-5 text-red-600">
                    {problems.map((p, i) => (
                      <li key={`${p}-${i}`}>{p}</li>
                    ))}
                  </ul>
                </>
              )}
            </div>
            <div className="space-y-1">
              <div className="text-sm font-medium">
                Excluded signals <InfoTip term="exclusion" />
              </div>
              {exclusions.length === 0 ? (
                <p className="text-sm text-muted-foreground">No signals were excluded.</p>
              ) : (
                <Table data-testid="ledger-exclusions">
                  <TableHeader>
                    <TableRow>
                      <TableHead>Reason code</TableHead>
                      <TableHead>Meaning</TableHead>
                      <TableHead className="text-right">Count</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {exclusions.map((e) => (
                      <TableRow key={e.code}>
                        <TableCell className="font-mono text-xs">{e.code}</TableCell>
                        <TableCell>{e.label}</TableCell>
                        <TableCell className="text-right font-mono">{e.count}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}
