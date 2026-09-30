import type { Baseline, RunRecord } from '@/api/backtestResults'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import {
  exceedsSlotLimit,
  fmtNum,
  fmtSigned,
  isKnown,
  metricText,
  SLOT_LIMIT,
  signClass,
  signsDisagree,
} from '@/lib/backtestResults'
import { cn } from '@/lib/utils'

function Note({ testId, children }: { testId: string; children: React.ReactNode }) {
  return (
    <div
      role="note"
      data-testid={testId}
      className="rounded-md border border-yellow-500/50 p-3 text-sm"
    >
      {children}
    </div>
  )
}

function PositionCaps({ run }: { run: RunRecord }) {
  const m = run.metrics
  const caps = [...(run.position_caps ?? [])].sort((a, b) => a.max_positions - b.max_positions)
  if (caps.length === 0) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="no-caps">
        Position caps were not recorded for this run.
      </p>
    )
  }
  return (
    <div className="space-y-3">
      {exceedsSlotLimit(m) && (
        <Note testId="slot-warning">
          Needed up to {fmtNum(m.peak_concurrent, 0)} open positions at once (typically{' '}
          {fmtNum(m.median_concurrent, 0)}). The unlimited-position result cannot be traded by an
          account with {SLOT_LIMIT} or fewer slots: read the capped rows instead.
        </Note>
      )}
      <Table data-testid="position-caps">
        <TableHeader>
          <TableRow>
            <TableHead>Slots</TableHead>
            <TableHead className="text-right">Trades taken</TableHead>
            <TableHead className="text-right">Share of signals</TableHead>
            <TableHead className="text-right">Net bps per trade</TableHead>
            <TableHead className="text-right">t-statistic</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {caps.map((c) => (
            <TableRow key={c.max_positions}>
              <TableCell>{c.max_positions}</TableCell>
              <TableCell className="text-right font-mono">{c.trades}</TableCell>
              <TableCell className="text-right font-mono">
                {c.taken_pct === undefined ? 'n/a' : `${fmtNum(c.taken_pct, 1)}%`}
              </TableCell>
              <TableCell className={cn('text-right font-mono', signClass(c.net_bps, c.trades > 0))}>
                {c.trades > 0 ? fmtNum(c.net_bps) : 'n/a'}
              </TableCell>
              <TableCell className="text-right font-mono">{fmtNum(c.t_stat)}</TableCell>
            </TableRow>
          ))}
          <TableRow>
            <TableCell>Unlimited</TableCell>
            <TableCell className="text-right font-mono">{m.trades}</TableCell>
            <TableCell className="text-right font-mono">100.0%</TableCell>
            <TableCell
              className={cn('text-right font-mono', signClass(m.net_bps, isKnown(m, m.net_bps)))}
            >
              {metricText(m, m.net_bps)}
            </TableCell>
            <TableCell className="text-right font-mono">{metricText(m, m.t_stat)}</TableCell>
          </TableRow>
        </TableBody>
      </Table>
    </div>
  )
}

function BaselineBlock({ run, baseline }: { run: RunRecord; baseline: Baseline }) {
  const strategy = baseline.trade_net_bps ?? run.metrics.net_bps
  const excess = baseline.excess_net_bps
  const direction = excess >= 0 ? 'more' : 'less'
  return (
    <div className="space-y-2 text-sm" data-testid="baseline">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <div className="rounded-md border p-3">
          <div className="text-xs text-muted-foreground">Strategy net bps per trade</div>
          <div className="font-mono text-lg">{fmtNum(strategy)}</div>
        </div>
        <div className="rounded-md border p-3">
          <div className="text-xs text-muted-foreground">Random entry net bps per trade</div>
          <div className="font-mono text-lg">{fmtNum(baseline.net_bps)}</div>
        </div>
        <div className="rounded-md border p-3">
          <div className="text-xs text-muted-foreground">Excess over random entry</div>
          <div className={cn('font-mono text-lg', signClass(excess))}>{fmtSigned(excess)}</div>
          <div className="text-xs text-muted-foreground">
            t-statistic {fmtNum(baseline.excess_t_stat)}
          </div>
        </div>
      </div>
      <p>
        The strategy earned {fmtNum(Math.abs(excess))} bps per trade {direction} than random entry
        on the same names, direction and holding period. A long-only strategy tested through a
        rising market earns the market whether or not its signal adds anything.
      </p>
      {baseline.skipped ? (
        <p className="text-muted-foreground">
          {baseline.skipped} trades could not be matched and are excluded from both sides.
        </p>
      ) : null}
      {baseline.method && <p className="text-xs text-muted-foreground">{baseline.method}</p>}
    </div>
  )
}

/**
 * Checks that separate a per-trade edge from one a real account could capture: how many
 * positions it needs at once, and what random entry would have earned anyway.
 */
export function RealityChecks({ run }: { run: RunRecord }) {
  return (
    <Card data-testid="reality-checks">
      <CardHeader>
        <CardTitle>Reality checks</CardTitle>
      </CardHeader>
      <CardContent className="space-y-6">
        {signsDisagree(run.metrics) && (
          <Note testId="sign-note">
            The per-trade average and the t-statistic point opposite ways. The average counts every
            trade; the t-statistic counts each trading day once, so a few heavy signal days may be
            carrying the result.
          </Note>
        )}
        <section className="space-y-2">
          <h3 className="text-sm font-semibold">What a limited number of open positions leaves</h3>
          <PositionCaps run={run} />
        </section>
        <section className="space-y-2">
          <h3 className="text-sm font-semibold">Against random entry</h3>
          {run.baseline ? (
            <BaselineBlock run={run} baseline={run.baseline} />
          ) : (
            <p className="text-sm text-muted-foreground" data-testid="no-baseline">
              Random-entry baseline not computed for this run (intraday runs skip it).
            </p>
          )}
        </section>
      </CardContent>
    </Card>
  )
}
