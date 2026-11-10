import type { RunDetail, RunRecord, TradeSubset } from '@/api/backtestResults'
import { fmtNum } from '@/lib/backtestResults'
import { AssumptionsConfidence } from './AssumptionsConfidence'
import { CostDecomposition } from './CostDecomposition'
import { ExecutionWarningsBlock } from './ExecutionWarningsBlock'
import { runBlocks } from './engineBlocks'
import { MetricTip } from './MetricTip'

/**
 * How the result was filled and charged: gross to costs to net, the slippage assumed, the
 * trades that gapped through a stop, and the full assumptions panel.
 */
export function ExecutionDiagnostics({
  run,
  detail,
  onShowTrades,
}: {
  run: RunRecord
  detail: RunDetail | null | undefined
  onShowTrades?: (subset: TradeSubset) => void
}) {
  const slip = detail?.scope?.slippage_bps_per_side
  return (
    <div className="space-y-6" data-testid="execution-diagnostics">
      <CostDecomposition
        costComponents={detail?.captured ? (detail.cost_components ?? null) : null}
        grossBps={run.metrics.gross_bps}
        netBps={run.metrics.net_bps}
      />
      <p className="text-sm" data-testid="slippage-line">
        Slippage assumed
        <MetricTip metric="slippage" />:{' '}
        {typeof slip === 'number' ? `${fmtNum(slip, 1)} bps per side` : 'not recorded'}.
        {onShowTrades && (
          <>
            {' '}
            <button
              type="button"
              className="underline underline-offset-4"
              onClick={() => onShowTrades('gap_exits')}
            >
              Show trades that gapped through the stop
            </button>
          </>
        )}
      </p>
      <ExecutionWarningsBlock warnings={runBlocks(run, detail).warnings} />
      <AssumptionsConfidence run={run} detail={detail} />
    </div>
  )
}
