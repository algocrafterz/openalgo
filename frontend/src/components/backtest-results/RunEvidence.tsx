import type { RunDetail, RunRecord, TradeSubset } from '@/api/backtestResults'
import { CostDecomposition } from './CostDecomposition'
import { DataQualityBlock } from './DataQualityBlock'
import { IntegrityChecks } from './IntegrityChecks'
import { LedgerSummaryBlock } from './LedgerSummaryBlock'
import { TradesDrilldown } from './TradesDrilldown'
import { TrialsBlock } from './TrialsBlock'

export interface TradesDrillState {
  subset: TradeSubset
  /** Changes on every open so the drill-down remounts on the requested subset. */
  nonce: number
}

/**
 * The evidence behind a run: integrity checks, data quality, ledgers, trials, cost, and the trade
 * drill-down. A run recorded before these existed shows each block's own "not captured" text.
 */
export function RunEvidence({
  ideaId,
  run,
  detail,
  drill,
  onOpenTrades,
  onCloseTrades,
}: {
  ideaId: string
  run: RunRecord
  detail: RunDetail
  drill: TradesDrillState | null
  onOpenTrades: (subset: TradeSubset) => void
  onCloseTrades: () => void
}) {
  const c = detail.captured
  return (
    <>
      <IntegrityChecks validity={c ? (detail.validity ?? null) : null} />
      <DataQualityBlock dataQuality={c ? (detail.data_quality ?? null) : null} />
      <LedgerSummaryBlock ledgers={c ? (detail.ledgers ?? null) : null} />
      <TrialsBlock trials={c ? (detail.trials ?? null) : null} />
      <CostDecomposition
        costComponents={c ? (detail.cost_components ?? null) : null}
        grossBps={run.metrics.gross_bps}
        netBps={run.metrics.net_bps}
      />
      <section id="trades" data-testid="trades-section" className="space-y-2">
        {drill ? (
          <TradesDrilldown
            key={`${run.run_id}-${drill.nonce}`}
            ideaId={ideaId}
            runId={run.run_id}
            hasTrades={c && !!detail.has_trades}
            hasLedgers={c && !!detail.has_ledgers}
            initialSubset={drill.subset}
            onClose={onCloseTrades}
          />
        ) : (
          <div className="flex items-center gap-3">
            <h2 className="text-base font-semibold">Trades</h2>
            <button
              type="button"
              className="rounded border px-2 py-0.5 text-sm"
              onClick={() => onOpenTrades('all')}
            >
              Open trades
            </button>
          </div>
        )}
      </section>
    </>
  )
}
