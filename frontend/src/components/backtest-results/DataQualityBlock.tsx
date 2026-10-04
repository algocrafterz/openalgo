import type { DataQualityReport } from '@/api/backtestResults'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { cn } from '@/lib/utils'
import { InfoTip } from './InfoTip'

const STATUS_CLASS: Record<string, string> = {
  PASS: 'border-green-600/50 text-green-600',
  WARNING: 'border-amber-600/60 text-amber-700',
  NOT_RUN: 'border-dashed border-muted-foreground/50 text-muted-foreground italic',
}

const CLEAN_LABELS: [string, string][] = [
  ['duplicate_ts', 'Duplicate timestamps'],
  ['outside_hours', 'Rows outside trading hours'],
  ['impossible', 'Impossible rows'],
]

const POLICY_LABELS: [keyof DataQualityReport['policy'], string][] = [
  ['corporate_actions', 'Corporate actions'],
  ['closing_auction', 'Closing auction'],
  ['timestamps', 'Timestamps'],
]

function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {}
}

/** Sum one counter across the per-symbol dicts the backend sent. */
function sumClean(clean: Record<string, unknown>, key: string): number {
  return Object.values(clean).reduce<number>((acc, v) => {
    const n = asRecord(v)[key]
    return acc + (typeof n === 'number' ? n : 0)
  }, 0)
}

function sumDays(split: Record<string, unknown>): number {
  return Object.values(split).reduce<number>((acc, v) => acc + (Array.isArray(v) ? v.length : 0), 0)
}

function DroppedCounts({ dropped }: { dropped: Record<string, unknown> }) {
  const clean = asRecord(dropped.clean_dropped)
  const split = asRecord(dropped.split_days_dropped)
  const minSessions = asRecord(dropped.symbols_dropped_min_sessions)
  return (
    <div data-testid="dq-dropped" className="space-y-1">
      <div className="text-sm font-medium">Data dropped before the run</div>
      <dl className="grid grid-cols-[auto_auto] justify-start gap-x-6 gap-y-0.5 text-sm">
        {CLEAN_LABELS.map(([key, label]) => (
          <div key={key} className="contents">
            <dt className="text-muted-foreground">{label}</dt>
            <dd className="font-mono" data-testid={`dropped-${key}`}>
              {sumClean(clean, key)}
            </dd>
          </div>
        ))}
        <dt className="text-muted-foreground">Split days excluded</dt>
        <dd className="font-mono" data-testid="dropped-split_days">
          {sumDays(split)}
        </dd>
        <dt className="text-muted-foreground">Symbols dropped (too few sessions)</dt>
        <dd className="font-mono" data-testid="dropped-symbols">
          {Object.keys(minSessions).length}
        </dd>
      </dl>
    </div>
  )
}

/** The data audit for a run, with what it dropped and what the data layer does not handle. */
export function DataQualityBlock({ dataQuality }: { dataQuality: DataQualityReport | null }) {
  const dq = dataQuality
  const symbols = typeof dq?.summary?.symbols === 'number' ? dq.summary.symbols : null
  const audited = Array.isArray(dq?.summary?.audited_symbols) ? dq.summary.audited_symbols : null
  const sampled = symbols !== null && audited !== null && audited.length < symbols
  return (
    <Card data-testid="data-quality" className="max-w-3xl">
      <CardHeader>
        <CardTitle className="text-base">
          Data quality <InfoTip term="data_quality" />
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {!dq ? (
          <p className="text-sm text-muted-foreground">Not captured for this run</p>
        ) : (
          <>
            <span
              data-testid="dq-status"
              className={cn(
                'inline-block rounded border px-2 py-0.5 text-sm',
                STATUS_CLASS[dq.status] ?? 'border-border text-muted-foreground'
              )}
            >
              {dq.status}
            </span>
            {dq.affects_validity && (
              <p data-testid="dq-validity" className="text-sm text-amber-700">
                This warning makes the run invalid, so the verdict is INCONCLUSIVE.
              </p>
            )}
            {sampled && (
              <p data-testid="dq-sample" className="text-sm text-muted-foreground">
                Audited a sample of {audited.length} of {symbols} symbols.
              </p>
            )}
            {(dq.items ?? []).length === 0 ? (
              <p className="text-sm text-muted-foreground">No data-quality items were reported.</p>
            ) : (
              <Table data-testid="dq-items">
                <TableHeader>
                  <TableRow>
                    <TableHead>Check</TableHead>
                    <TableHead>Severity</TableHead>
                    <TableHead>Detail</TableHead>
                    <TableHead className="text-right">Count</TableHead>
                    <TableHead>Affected symbols</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {(dq.items ?? []).map((it, i) => (
                    <TableRow key={`${it.check}-${i}`}>
                      <TableCell className="font-mono text-xs">{it.check}</TableCell>
                      <TableCell>{it.severity}</TableCell>
                      <TableCell className="whitespace-normal break-words">{it.detail}</TableCell>
                      <TableCell className="text-right font-mono">{it.count}</TableCell>
                      <TableCell className="whitespace-normal break-words">
                        {(it.symbols ?? []).join(', ') || 'none listed'}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
            <DroppedCounts dropped={asRecord(dq.dropped)} />
            <div className="space-y-1">
              <div id="dq-policy-title" className="text-sm font-medium">
                What the data layer does and does not handle
              </div>
              <ul aria-labelledby="dq-policy-title" className="list-disc pl-5 text-sm space-y-0.5">
                {POLICY_LABELS.map(([key, label]) => (
                  <li key={key}>
                    <span className="font-medium">{label}: </span>
                    {dq.policy?.[key] ?? 'Not stated'}
                  </li>
                ))}
              </ul>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}
