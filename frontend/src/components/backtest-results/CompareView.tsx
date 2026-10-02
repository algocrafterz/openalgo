import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import {
  type BacktestCompareResponse,
  backtestResultsApi,
  type MetricDelta,
  type RunRecord,
} from '@/api/backtestResults'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { fmtNum, fmtPercent, fmtSigned, METRIC_LABELS, NA } from '@/lib/backtestResults'
import { CompareDetail } from './CompareDetail'
import { ExportMenu } from './ExportMenu'
import { LineChart } from './LineChart'

const COLOR_A = '#3b82f6'
const COLOR_B = '#f97316'
const COUNT_KEYS = new Set(['trades', 'days'])

function formatValue(key: string, value: number | null): string {
  if (value === null) return NA
  if (key === 'win_rate') return fmtPercent(value)
  return COUNT_KEYS.has(key) ? String(value) : fmtNum(value)
}

function formatDelta(key: string, delta: number | null): string {
  if (delta === null) return NA
  if (key === 'win_rate') return `${fmtSigned(delta * 100, 1)} pts`
  return COUNT_KEYS.has(key) ? fmtSigned(delta, 0) : fmtSigned(delta)
}

function orderedKeys(deltas: Record<string, MetricDelta>): string[] {
  const known = Object.keys(METRIC_LABELS).filter((k) => k in deltas)
  const rest = Object.keys(deltas)
    .filter((k) => !(k in METRIC_LABELS))
    .sort()
  return [...known, ...rest]
}

function DeltaTable({ deltas }: { deltas: Record<string, MetricDelta> }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Metric</TableHead>
          <TableHead className="text-right">Run A</TableHead>
          <TableHead className="text-right">Run B</TableHead>
          <TableHead className="text-right">Change</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {orderedKeys(deltas).map((key) => {
          const d = deltas[key]
          return (
            <TableRow key={key}>
              <TableCell>{METRIC_LABELS[key] ?? key}</TableCell>
              <TableCell className="text-right font-mono">{formatValue(key, d.a)}</TableCell>
              <TableCell className="text-right font-mono">{formatValue(key, d.b)}</TableCell>
              <TableCell className="text-right font-mono text-muted-foreground">
                {formatDelta(key, d.delta)}
              </TableCell>
            </TableRow>
          )
        })}
      </TableBody>
    </Table>
  )
}

function ParamDiffView({ diff }: { diff: NonNullable<BacktestCompareResponse['params']> }) {
  const changed = Object.entries(diff.changed)
  const added = Object.entries(diff.added)
  const removed = Object.entries(diff.removed)
  if (changed.length + added.length + removed.length === 0) {
    return <div className="text-sm text-muted-foreground">Parameters are identical</div>
  }
  const show = (v: unknown) => (typeof v === 'string' ? v : JSON.stringify(v))
  return (
    <ul className="text-sm space-y-1 font-mono">
      {changed.map(([k, v]) => (
        <li key={`c-${k}`}>
          changed {k}: {show(v.a)} to {show(v.b)}
        </li>
      ))}
      {added.map(([k, v]) => (
        <li key={`a-${k}`}>
          added {k}: {show(v)}
        </li>
      ))}
      {removed.map(([k, v]) => (
        <li key={`r-${k}`}>
          removed {k} (was {show(v)})
        </li>
      ))}
    </ul>
  )
}

function CompareBody({ data, ideaId }: { data: BacktestCompareResponse; ideaId: string }) {
  return (
    <div className="space-y-6" data-testid="compare-body">
      <div className="space-y-1">
        {data.run_a && (
          <ExportMenu ideaId={ideaId} runId={data.run_a.run_id} label="Export run A" />
        )}
        {data.run_b && (
          <ExportMenu ideaId={ideaId} runId={data.run_b.run_id} label="Export run B" />
        )}
      </div>
      {data.comparison && !(data.run_a && data.run_b) ? (
        <div role="alert" className="text-sm text-red-600">
          The detailed comparison could not be shown: the response is missing one of the two runs.
        </div>
      ) : data.comparison && data.run_a && data.run_b ? (
        <CompareDetail comparison={data.comparison} runA={data.run_a} runB={data.run_b} />
      ) : (
        <LegacyBody data={data} />
      )}
    </div>
  )
}

function LegacyBody({ data }: { data: BacktestCompareResponse }) {
  const trials = data.n_trials
  return (
    <div className="space-y-6">
      {data.comparison_note && (
        <p className="text-sm text-muted-foreground" data-testid="comparison-note">
          {data.comparison_note}
        </p>
      )}
      {data.comparable === false && (
        <div role="alert" className="rounded-md border border-yellow-500/60 p-3 text-sm space-y-1">
          <div className="font-medium">
            These runs are not like-for-like, so the differences below mix a strategy change with a
            change in the test itself.
          </div>
          <ul className="list-disc pl-5">
            {(data.warnings ?? []).map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </div>
      )}
      {(data.info ?? []).length > 0 && (
        <ul className="flex flex-wrap gap-2 text-xs">
          {(data.info ?? []).map((i) => (
            <li key={i} className="rounded-full border px-2 py-0.5 text-muted-foreground">
              {i}
            </li>
          ))}
        </ul>
      )}
      {data.metric_deltas && <DeltaTable deltas={data.metric_deltas} />}
      {trials && (
        <p className="text-sm">
          Variants tried: run A {trials.a}, run B {trials.b}.{' '}
          <span className="text-muted-foreground">
            More variants tried means a better-looking result is more likely to be luck.
          </span>
        </p>
      )}
      {data.params && (
        <div>
          <div className="text-sm font-medium mb-1">Parameter changes</div>
          <ParamDiffView diff={data.params} />
        </div>
      )}
      {data.equity_r && (
        <LineChart
          title="Cumulative net R, both runs"
          series={[
            { label: `A: ${data.run_a?.date ?? ''}`, color: COLOR_A, points: data.equity_r.a },
            { label: `B: ${data.run_b?.date ?? ''}`, color: COLOR_B, points: data.equity_r.b },
          ]}
        />
      )}
    </div>
  )
}

export function CompareView(props: { ideaId: string; runs: RunRecord[] }) {
  // Keyed so the selected runs never point at runs that no longer exist.
  const key = `${props.ideaId}|${props.runs.map((r) => r.run_id).join(',')}`
  return <CompareViewInner key={key} {...props} />
}

function CompareViewInner({ ideaId, runs }: { ideaId: string; runs: RunRecord[] }) {
  const [runA, setRunA] = useState(runs.length >= 2 ? runs[runs.length - 2].run_id : '')
  const [runB, setRunB] = useState(runs.length >= 2 ? runs[runs.length - 1].run_id : '')
  const enabled = runs.length >= 2 && runA !== '' && runB !== '' && runA !== runB

  const { data, isLoading } = useQuery({
    queryKey: ['backtest-results', ideaId, 'compare', runA, runB],
    queryFn: () => backtestResultsApi.compare(ideaId, runA, runB),
    enabled,
  })

  const picker = (label: string, value: string, onChange: (v: string) => void) => (
    <label className="text-sm flex items-center gap-2">
      {label}
      <select
        className="border-input bg-background rounded-md border px-2 py-1 text-sm"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        {runs.map((r) => (
          <option key={r.run_id} value={r.run_id}>
            {r.date} - {r.run_id}
          </option>
        ))}
      </select>
    </label>
  )

  return (
    <Card>
      <CardHeader>
        <CardTitle>Compare runs</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {runs.length < 2 ? (
          <div className="text-sm text-muted-foreground">
            Only one run is recorded. After the strategy is tuned and back-tested again, the two
            runs can be compared here.
          </div>
        ) : (
          <>
            <div className="flex flex-wrap gap-4">
              {picker('Run A', runA, setRunA)}
              {picker('Run B', runB, setRunB)}
            </div>
            {!enabled && (
              <div className="text-sm text-muted-foreground">
                Pick two different runs to compare.
              </div>
            )}
            {enabled && isLoading && (
              <div className="text-sm text-muted-foreground">Comparing...</div>
            )}
            {enabled && data?.status === 'error' && (
              <div role="alert" className="text-sm text-red-600">
                {data.message ?? 'Comparison failed'}
              </div>
            )}
            {enabled && data?.status === 'success' && <CompareBody data={data} ideaId={ideaId} />}
          </>
        )}
      </CardContent>
    </Card>
  )
}
