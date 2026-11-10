import type { ExecutionWarnings } from '@/api/backtestResults'
import { fmtNum } from '@/lib/backtestResults'
import { NOT_RECORDED_FOR_RUN, pct } from './engineBlocks'
import { InfoTip } from './InfoTip'

const r = (v: number | null | undefined) =>
  typeof v === 'number' && Number.isFinite(v) ? `${fmtNum(v, 2)} R` : 'n/a'

/**
 * Level 4: trades that rest on a guess about the bars. The same-bar stop-and-target figure is an
 * upper bound shown beside the actual one, never a result. Nothing is computed here.
 */
export function ExecutionWarningsBlock({ warnings }: { warnings: ExecutionWarnings | null }) {
  if (!warnings) {
    return (
      <section className="space-y-1" data-testid="execution-warnings">
        <h3 className="text-base font-semibold">Execution warnings</h3>
        <p className="text-sm text-muted-foreground">Execution warnings: {NOT_RECORDED_FOR_RUN}.</p>
      </section>
    )
  }
  const s = warnings.ambiguous_sensitivity
  const measured = !!s && s.actual_mean_net_r !== null && s.upper_bound_mean_net_r !== null
  return (
    <section className="space-y-3" data-testid="execution-warnings">
      <h3 className="text-base font-semibold">Execution warnings</h3>
      <ul className="space-y-1 text-sm">
        <li data-testid="ew-ambiguous">
          <span className="text-muted-foreground">
            Trades where one bar reached both stop and target
            <InfoTip term="ambiguous_exit" />:{' '}
          </span>
          {warnings.ambiguous_exit_trades} of {warnings.trades} (
          {pct(warnings.ambiguous_exit_share)}). Each was counted as stopped out.
        </li>
        <li data-testid="ew-gap">
          <span className="text-muted-foreground">
            Trades open while a bar was missing
            <InfoTip term="data_gap_in_trade" />:{' '}
          </span>
          {warnings.data_gap_trades} of {warnings.trades} ({pct(warnings.data_gap_share)}).
        </li>
        <li data-testid="ew-skipped">
          <span className="text-muted-foreground">
            Entries skipped because the bar was missing:{' '}
          </span>
          {warnings.entries_skipped_missing_bar === null ||
          warnings.entries_skipped_missing_bar === undefined
            ? NOT_RECORDED_FOR_RUN
            : warnings.entries_skipped_missing_bar}
        </li>
      </ul>
      {s && (
        <div className="space-y-1 rounded-md border p-3 text-sm" data-testid="ew-sensitivity">
          <h4 className="font-medium">If every same-bar collision had hit the target</h4>
          {measured ? (
            <>
              <p>
                Those {s.trades ?? warnings.ambiguous_exit_trades} trades averaged{' '}
                <span className="font-mono">{r(s.actual_mean_net_r)}</span> net as run (stop taken).
                Upper bound if all had exited at the target:{' '}
                <span className="font-mono">{r(s.upper_bound_mean_net_r)}</span>.
              </p>
              {s.run_mean_net_r_actual != null && s.run_mean_net_r_upper_bound != null && (
                <p>
                  Whole run, average net per trade: {r(s.run_mean_net_r_actual)} as run, at most{' '}
                  {r(s.run_mean_net_r_upper_bound)} on that assumption.
                </p>
              )}
              <p className="text-muted-foreground" data-testid="ew-exact">
                {s.exact
                  ? 'Exact: each of these trades has one exit and the target it carried.'
                  : 'Approximate: some of these trades exit in parts or the target moves, so the upper bound is not exact.'}
              </p>
            </>
          ) : (
            <p className="text-muted-foreground">
              No trade could be measured this way, so there is no upper bound to show.
            </p>
          )}
          <p className="text-muted-foreground">
            An upper bound is the most the uncertainty could be hiding. It is not a result and not
            an estimate: the figures above count the stop.
          </p>
        </div>
      )}
    </section>
  )
}
