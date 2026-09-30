import { type ExportFormat, exportUrl } from '@/api/backtestResults'

interface ExportOption {
  format: ExportFormat
  label: string
  hint: string
}

const OPTIONS: ExportOption[] = [
  { format: 'json', label: 'JSON', hint: 'Canonical run record, exactly as stored' },
  { format: 'metrics', label: 'Metrics CSV', hint: 'Headline, in-sample, out-of-sample, caps' },
  { format: 'series', label: 'Series CSV', hint: 'Daily equity and drawdown in R' },
]

/**
 * Downloads of one run. JSON is the canonical result; the CSVs are flat views of it.
 * Per-trade rows are offered only when a file was stored beside the record, because run
 * records never hold trade lists.
 */
export function ExportMenu({
  ideaId,
  runId,
  hasTrades = false,
  label = 'Export run',
}: {
  ideaId: string
  runId: string
  hasTrades?: boolean
  label?: string
}) {
  const options: ExportOption[] = hasTrades
    ? [...OPTIONS, { format: 'trades', label: 'Trades CSV', hint: 'One row per trade, as stored' }]
    : OPTIONS
  return (
    <div className="flex flex-wrap items-center gap-2" data-testid={`export-${runId}`}>
      <span className="text-sm text-muted-foreground">{label}:</span>
      {options.map((o) => (
        <a
          key={o.format}
          href={exportUrl(ideaId, runId, o.format)}
          download
          title={o.hint}
          className="text-sm underline underline-offset-4"
        >
          {o.label}
        </a>
      ))}
    </div>
  )
}
