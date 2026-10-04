import { useEffect, useRef, useState } from 'react'
import type { TradeRow, TradeSubset, TradesQuery } from '@/api/backtestResults'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { useRunTrades } from '@/hooks/useRunTrades'
import { TRADE_SUBSET_LABELS, TRADE_SUBSETS } from '@/lib/backtestEvidence'
import { fmtNum } from '@/lib/backtestResults'
import { InfoTip } from './InfoTip'
import { TradeLineagePanel } from './TradeLineagePanel'

const COLUMNS: { key: string; label: string; numeric?: boolean }[] = [
  { key: 'trade_id', label: 'Trade id' },
  { key: 'symbol', label: 'Symbol' },
  { key: 'side', label: 'Side' },
  { key: 'entry_time', label: 'Entry time' },
  { key: 'exit_time', label: 'Exit time' },
  { key: 'entry_price', label: 'Entry price', numeric: true },
  { key: 'exit_price', label: 'Exit price', numeric: true },
  { key: 'exit_reason', label: 'Exit reason' },
  { key: 'net_r', label: 'Net R', numeric: true },
  { key: 'net_bps', label: 'Net bps', numeric: true },
  { key: 'cost_bps', label: 'Cost bps', numeric: true },
]

const PAGE_SIZES = [25, 50, 100, 200]
const DEBOUNCE_MS = 300

/** Commits a typed value after a pause, resetting the page in the same update. */
function useCommitted(text: string, onCommit: (v: string) => void, ms: number) {
  const cb = useRef(onCommit)
  cb.current = onCommit
  const first = useRef(true)
  useEffect(() => {
    if (first.current) {
      first.current = false
      return
    }
    const t = setTimeout(() => cb.current(text), ms)
    return () => clearTimeout(t)
  }, [text, ms])
}

function cell(row: TradeRow, key: string, numeric?: boolean): string {
  const v = row[key]
  if (v === null || v === undefined) return 'n/a'
  if (numeric && typeof v === 'number') return fmtNum(v, key.endsWith('price') ? 2 : 2)
  return String(v)
}

function SortHead({
  col,
  sort,
  desc,
  onSort,
}: {
  col: (typeof COLUMNS)[number]
  sort: string | null
  desc: boolean
  onSort: (key: string) => void
}) {
  const active = sort === col.key
  const aria = active ? (desc ? 'descending' : 'ascending') : 'none'
  return (
    <TableHead aria-sort={aria} className={col.numeric ? 'text-right' : undefined}>
      <button
        type="button"
        className="underline-offset-4 hover:underline"
        onClick={() => onSort(col.key)}
      >
        {col.label}
      </button>
      {active && (
        <span data-testid="sort-indicator" aria-hidden="true" className="ml-1 text-xs">
          {desc ? 'desc' : 'asc'}
        </span>
      )}
    </TableHead>
  )
}

/** Paged, filterable trade list with a lineage panel. Everything is read from the API. */
export function TradesDrilldown({
  ideaId,
  runId,
  hasTrades,
  hasLedgers,
  initialSubset = 'all',
  onClose,
}: {
  ideaId: string
  runId: string
  hasTrades: boolean
  hasLedgers: boolean
  initialSubset?: TradeSubset
  onClose?: () => void
}) {
  const [subset, setSubset] = useState<TradeSubset>(initialSubset)
  const [symbolText, setSymbolText] = useState('')
  const [reasonText, setReasonText] = useState('')
  const [limit, setLimit] = useState(50)
  const [offset, setOffset] = useState(0)
  const [sort, setSort] = useState<string | null>(null)
  const [desc, setDesc] = useState(false)
  const [selected, setSelected] = useState<TradeRow | null>(null)
  const [symbol, setSymbol] = useState('')
  const [reason, setReason] = useState('')
  const headingRef = useRef<HTMLHeadingElement>(null)
  const rootRef = useRef<HTMLDivElement>(null)

  // Every change that alters the result set returns to the first page in the same update, so no
  // request is made with a new filter and the old offset.
  useCommitted(
    symbolText,
    (v) => {
      setSymbol(v.trim().toUpperCase())
      setOffset(0)
    },
    DEBOUNCE_MS
  )
  useCommitted(
    reasonText,
    (v) => {
      setReason(v.trim())
      setOffset(0)
    },
    DEBOUNCE_MS
  )

  useEffect(() => {
    headingRef.current?.focus()
    headingRef.current?.scrollIntoView?.({ block: 'start' })
  }, [])

  // Focus returns to the trigger in an effect that runs after the panel has unmounted, so the
  // order is deterministic and does not depend on animation frames.
  const restoreFocusId = useRef<string | null>(null)
  const closeLineage = () => {
    restoreFocusId.current = selected?.trade_id ?? null
    setSelected(null)
  }
  useEffect(() => {
    const id = restoreFocusId.current
    if (selected !== null || id === null) return
    restoreFocusId.current = null
    rootRef.current?.querySelector<HTMLButtonElement>(`[data-trigger="${CSS.escape(id)}"]`)?.focus()
  }, [selected])

  const query: TradesQuery = {
    subset,
    ...(symbol ? { symbol } : {}),
    ...(reason ? { reason } : {}),
    ...(sort ? { sort, desc } : {}),
    limit,
    offset,
  }
  const q = useRunTrades(ideaId, runId, query, hasTrades)

  const onSort = (key: string) => {
    setOffset(0)
    if (sort === key) setDesc((d) => !d)
    else {
      setSort(key)
      setDesc(false)
    }
  }

  const stale = q.isPlaceholderData
  const filtered = symbol !== '' || reason !== ''
  const page = q.data?.page
  const failed = q.isError || q.data?.status === 'error'
  const rows = page?.rows ?? []
  const total = page?.total ?? 0
  const from = rows.length === 0 ? 0 : (page?.offset ?? offset) + 1
  const to = (page?.offset ?? offset) + rows.length

  return (
    <div ref={rootRef}>
      <Card data-testid="trades-drilldown">
        <CardHeader className="flex-row items-center justify-between">
          <CardTitle className="text-base">
            <h2 ref={headingRef} tabIndex={-1} className="inline outline-none">
              Trades
            </h2>
            <InfoTip term="trade_subsets" />
          </CardTitle>
          {onClose && (
            <button type="button" className="text-sm underline" onClick={onClose}>
              Close trades
            </button>
          )}
        </CardHeader>
        <CardContent className="space-y-3">
          {!hasTrades ? (
            <p className="text-sm text-muted-foreground">Trades were not recorded for this run</p>
          ) : (
            <>
              <div className="flex flex-wrap items-end gap-3 text-sm">
                <label className="flex flex-col gap-0.5">
                  Subset
                  <select
                    className="rounded border bg-background px-2 py-1"
                    value={subset}
                    onChange={(e) => {
                      setSubset(e.target.value as TradeSubset)
                      setOffset(0)
                    }}
                  >
                    {TRADE_SUBSETS.map((s) => (
                      <option key={s} value={s}>
                        {TRADE_SUBSET_LABELS[s]}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="flex flex-col gap-0.5">
                  Symbol (exact)
                  <input
                    className="rounded border bg-background px-2 py-1"
                    value={symbolText}
                    onChange={(e) => setSymbolText(e.target.value)}
                  />
                </label>
                <label className="flex flex-col gap-0.5">
                  Exit reason (exact)
                  <input
                    className="rounded border bg-background px-2 py-1"
                    value={reasonText}
                    onChange={(e) => setReasonText(e.target.value)}
                  />
                </label>
                <label className="flex flex-col gap-0.5">
                  Page size
                  <select
                    className="rounded border bg-background px-2 py-1"
                    value={limit}
                    onChange={(e) => {
                      setLimit(Number(e.target.value))
                      setOffset(0)
                    }}
                  >
                    {PAGE_SIZES.map((n) => (
                      <option key={n} value={n}>
                        {n}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              {q.data?.page?.subset_note && (
                <p data-testid="subset-note" className="text-xs text-muted-foreground">
                  {q.data.page.subset_note}
                </p>
              )}
              <div data-testid="trades-body" aria-busy={stale || q.isLoading}>
                {failed ? (
                  <p role="alert" className="text-sm text-red-600">
                    Could not load trades
                  </p>
                ) : q.isLoading || stale ? (
                  <p className="text-sm text-muted-foreground">Loading trades</p>
                ) : rows.length === 0 ? (
                  <p className="text-sm text-muted-foreground">
                    {filtered
                      ? 'No trades match these filters (symbol and exit reason are exact matches)'
                      : (q.data?.message ?? 'No trades in this subset')}
                  </p>
                ) : (
                  <>
                    <Table>
                      <TableHeader>
                        <TableRow>
                          {COLUMNS.map((c) => (
                            <SortHead key={c.key} col={c} sort={sort} desc={desc} onSort={onSort} />
                          ))}
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {rows.map((r) => (
                          <TableRow key={r.trade_id} data-testid={`trade-${r.trade_id}`}>
                            {COLUMNS.map((c) => (
                              <TableCell
                                key={c.key}
                                className={c.numeric ? 'text-right font-mono' : 'font-mono text-xs'}
                              >
                                {c.key === 'trade_id' ? (
                                  <button
                                    type="button"
                                    className="underline"
                                    aria-label={`Open lineage for ${r.trade_id}`}
                                    data-trigger={r.trade_id}
                                    onClick={() => setSelected(r)}
                                  >
                                    {r.trade_id}
                                  </button>
                                ) : (
                                  cell(r, c.key, c.numeric)
                                )}
                              </TableCell>
                            ))}
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                    <div className="flex items-center gap-3 text-sm">
                      <button
                        type="button"
                        className="rounded border px-2 py-0.5 disabled:opacity-50"
                        disabled={offset === 0}
                        onClick={() => setOffset(Math.max(0, offset - limit))}
                      >
                        Previous page
                      </button>
                      <span data-testid="trades-range">
                        {from}-{to} of {total}
                      </span>
                      <button
                        type="button"
                        className="rounded border px-2 py-0.5 disabled:opacity-50"
                        disabled={to >= total}
                        onClick={() => setOffset(offset + limit)}
                      >
                        Next page
                      </button>
                    </div>
                  </>
                )}
              </div>
              {selected && (
                <TradeLineagePanel
                  key={selected.trade_id}
                  ideaId={ideaId}
                  runId={runId}
                  row={selected}
                  hasLedgers={hasLedgers}
                  onClose={closeLineage}
                />
              )}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
