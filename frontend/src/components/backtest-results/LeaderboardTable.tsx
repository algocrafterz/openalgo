import { Link, useNavigate } from 'react-router'
import type { IdeaCard } from '@/api/backtestResults'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import {
  fmtAge,
  fmtNum,
  fmtSigned,
  fmtSpan,
  isKnown,
  metricText,
  NA,
  type SortDir,
  type SortKey,
  signClass,
  tVsHurdle,
} from '@/lib/backtestResults'
import { cn } from '@/lib/utils'
import { Sparkline } from './Sparkline'
import { StatusBadge, VerdictBadge } from './VerdictBadge'

interface Column {
  key: SortKey
  label: string
  align?: 'right'
}

const COLUMNS: Column[] = [
  { key: 'id', label: 'Idea' },
  { key: 'status', label: 'Status' },
  { key: 'verdict', label: 'Verdict' },
  { key: 'date', label: 'Latest run' },
  { key: 'runs', label: 'Runs', align: 'right' },
  { key: 'trades', label: 'Trades', align: 'right' },
  { key: 'gross', label: 'Gross bps', align: 'right' },
  { key: 'net', label: 'Net bps', align: 'right' },
]

const TAIL_COLUMNS: Column[] = [
  { key: 'oos', label: 'OOS net bps', align: 'right' },
  { key: 'trials', label: 'Variants', align: 'right' },
]

interface LeaderboardTableProps {
  ideas: IdeaCard[]
  sortKey: SortKey
  sortDir: SortDir
  onSort: (key: SortKey) => void
}

function ariaSort(active: boolean, dir: SortDir): 'ascending' | 'descending' | 'none' {
  if (!active) return 'none'
  return dir === 'asc' ? 'ascending' : 'descending'
}

function SortHeader({
  column,
  sortKey,
  sortDir,
  onSort,
}: {
  column: Column
  sortKey: SortKey
  sortDir: SortDir
  onSort: (key: SortKey) => void
}) {
  const active = sortKey === column.key
  return (
    <TableHead
      className={column.align === 'right' ? 'text-right' : undefined}
      aria-sort={ariaSort(active, sortDir)}
    >
      <button
        type="button"
        className="font-medium hover:underline"
        onClick={() => onSort(column.key)}
      >
        {column.label}
        {active ? (sortDir === 'asc' ? ' (asc)' : ' (desc)') : ''}
      </button>
    </TableHead>
  )
}

function ResultCells({ idea }: { idea: IdeaCard }) {
  const run = idea.latest_run
  if (!run) {
    return (
      <TableCell colSpan={9} className="text-muted-foreground text-sm">
        No run recorded
      </TableCell>
    )
  }
  const m = run.metrics
  return (
    <>
      <TableCell className="text-right font-mono">{m.trades}</TableCell>
      <TableCell className="text-right font-mono">{metricText(m, m.gross_bps)}</TableCell>
      <TableCell
        className={cn('text-right font-mono', signClass(m.net_bps, isKnown(m, m.net_bps)))}
      >
        {metricText(m, m.net_bps)}
      </TableCell>
      <TableCell className="text-right font-mono">{tVsHurdle(m)}</TableCell>
      <TableCell
        className={cn(
          'text-right font-mono',
          signClass(run.oos_net_bps, isKnown(m, run.oos_net_bps))
        )}
      >
        {metricText(m, run.oos_net_bps)}
      </TableCell>
      <TableCell className="text-right font-mono">{fmtNum(run.n_trials, 0)}</TableCell>
      <TableCell
        className={cn(
          'text-right font-mono',
          signClass(run.net_bps_cap10, isKnown(m, run.net_bps_cap10))
        )}
      >
        {metricText(m, run.net_bps_cap10)}
      </TableCell>
      <TableCell
        className={cn(
          'text-right font-mono',
          signClass(run.baseline_excess_bps, isKnown(m, run.baseline_excess_bps))
        )}
      >
        {isKnown(m, run.baseline_excess_bps) ? fmtSigned(run.baseline_excess_bps) : NA}
      </TableCell>
      <TableCell className="font-mono text-sm">
        <div>{run.period ? `${fmtSpan(run.years)} to ${run.period.end}` : NA}</div>
        <div className="text-xs text-muted-foreground">{`data ${fmtAge(run.data_age_days)}`}</div>
      </TableCell>
    </>
  )
}

export function LeaderboardTable({ ideas, sortKey, sortDir, onSort }: LeaderboardTableProps) {
  const navigate = useNavigate()
  return (
    <Table>
      <TableHeader>
        <TableRow>
          {COLUMNS.map((c) => (
            <SortHeader
              key={c.key}
              column={c}
              sortKey={sortKey}
              sortDir={sortDir}
              onSort={onSort}
            />
          ))}
          <TableHead className="text-right">t vs hurdle</TableHead>
          {TAIL_COLUMNS.map((c) => (
            <SortHeader
              key={c.key}
              column={c}
              sortKey={sortKey}
              sortDir={sortDir}
              onSort={onSort}
            />
          ))}
          <TableHead className="text-right">Net at 10 slots</TableHead>
          <TableHead className="text-right">vs random entry</TableHead>
          <TableHead>Tested</TableHead>
          <TableHead>Trend</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {ideas.map((idea) => (
          <TableRow
            key={idea.id}
            className="cursor-pointer"
            onClick={() => navigate(`/backtest-results/${idea.id}`)}
          >
            <TableCell>
              <Link
                to={`/backtest-results/${idea.id}`}
                className="font-medium hover:underline"
                onClick={(e) => e.stopPropagation()}
              >
                {idea.id}
              </Link>
              <div className="text-sm text-muted-foreground max-w-xs truncate">{idea.title}</div>
            </TableCell>
            <TableCell>
              <StatusBadge status={idea.status} />
            </TableCell>
            <TableCell>
              <VerdictBadge verdict={idea.verdict} />
            </TableCell>
            <TableCell className="font-mono">{idea.latest_run?.date ?? 'n/a'}</TableCell>
            <TableCell className="text-right font-mono">{idea.run_count}</TableCell>
            <ResultCells idea={idea} />
            <TableCell>
              {idea.latest_run ? <Sparkline values={idea.latest_run.sparkline} /> : null}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}
