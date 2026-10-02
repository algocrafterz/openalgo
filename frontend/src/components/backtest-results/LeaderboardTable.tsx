import { Link, useNavigate } from 'react-router'
import type { Classification, IdeaCard } from '@/api/backtestResults'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import {
  edgeBar,
  evidenceClass,
  fmtAge,
  fmtNum,
  fmtSigned,
  fmtSpan,
  ideaVerdict,
  isKnown,
  metricText,
  NA,
  type SortDir,
  type SortKey,
  THIN_SAMPLE_TRADES,
} from '@/lib/backtestResults'
import { cn } from '@/lib/utils'
import { Sparkline } from './Sparkline'
import { StatusBadge, VerdictBadge } from './VerdictBadge'

interface Column {
  key: SortKey
  label: string
  align?: 'right'
}

/** The columns that decide whether an idea is worth more time, in reading order. */
const LEAD_COLUMNS: Column[] = [
  { key: 'id', label: 'Idea' },
  { key: 'status', label: 'Status' },
  { key: 'verdict', label: 'Verdict (latest run)' },
]

const RESULT_COLUMNS: Column[] = [
  { key: 'trades', label: 'Trades', align: 'right' },
  { key: 'net', label: 'Net bps', align: 'right' },
]

const OOS_COLUMN: Column = { key: 'oos', label: 'OOS net bps', align: 'right' }

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

/** The figures that matter less than the verdict, kept to one muted line under the title. */
function ClassificationLine({ classification }: { classification?: Classification }) {
  if (!classification) return null
  const parts = [
    classification.strategy_category,
    classification.trading_style,
    classification.market,
    classification.timeframe,
  ].filter((p): p is string => Boolean(p))
  if (parts.length === 0) return null
  return (
    <div className="text-xs text-muted-foreground" data-testid="classification-line">
      {parts.join(' · ')}
    </div>
  )
}

function SecondaryLine({ idea }: { idea: IdeaCard }) {
  const run = idea.latest_run
  if (!run) return null
  const m = run.metrics
  const parts = [
    `latest ${run.date}`,
    `${idea.run_count} run${idea.run_count === 1 ? '' : 's'}`,
    `gross ${metricText(m, m.gross_bps)}`,
    `${fmtNum(run.n_trials, 0)} variant${run.n_trials === 1 ? '' : 's'}`,
    `net at 10 slots ${metricText(m, run.net_bps_cap10)}`,
    run.period ? `${fmtSpan(run.years)} to ${run.period.end}` : NA,
    `data ${fmtAge(run.data_age_days)}`,
  ]
  return (
    <div className="text-xs text-muted-foreground max-w-sm whitespace-normal">
      {parts.join(' | ')}
    </div>
  )
}

function ResultCells({ idea }: { idea: IdeaCard }) {
  const run = idea.latest_run
  if (!run) {
    return (
      <TableCell colSpan={6} className="text-muted-foreground text-sm">
        No run recorded
      </TableCell>
    )
  }
  const m = run.metrics
  const bar = edgeBar(m)
  const thin = m.trades > 0 && m.trades < THIN_SAMPLE_TRADES
  return (
    <>
      <TableCell className="font-mono hidden 2xl:table-cell">{run.date}</TableCell>
      <TableCell className="text-right font-mono">
        {m.trades}
        {thin && <div className="text-xs text-amber-600 font-sans">thin sample</div>}
      </TableCell>
      <TableCell
        className={cn(
          'text-right font-mono',
          evidenceClass(m.net_bps, m.t_stat, bar, isKnown(m, m.net_bps))
        )}
      >
        {metricText(m, m.net_bps)}
      </TableCell>
      <TableCell className="text-right font-mono">
        {isKnown(m, m.t_stat) ? `${fmtNum(m.t_stat)} vs ${fmtNum(bar)}` : NA}
      </TableCell>
      <TableCell
        className={cn(
          'text-right font-mono',
          evidenceClass(
            run.baseline_excess_bps,
            run.baseline_excess_t,
            bar,
            isKnown(m, run.baseline_excess_bps)
          )
        )}
      >
        {isKnown(m, run.baseline_excess_bps) ? fmtSigned(run.baseline_excess_bps) : NA}
      </TableCell>
      <TableCell
        className={cn(
          'text-right font-mono',
          evidenceClass(run.oos_net_bps, undefined, bar, isKnown(m, run.oos_net_bps))
        )}
      >
        {metricText(m, run.oos_net_bps)}
      </TableCell>
    </>
  )
}

export function LeaderboardTable({ ideas, sortKey, sortDir, onSort }: LeaderboardTableProps) {
  const navigate = useNavigate()
  const header = (c: Column) => (
    <SortHeader key={c.key} column={c} sortKey={sortKey} sortDir={sortDir} onSort={onSort} />
  )
  return (
    <Table>
      <TableHeader>
        <TableRow>
          {LEAD_COLUMNS.map(header)}
          <TableHead>Why</TableHead>
          <TableHead className="hidden 2xl:table-cell">
            <button
              type="button"
              className="font-medium hover:underline"
              onClick={() => onSort('date')}
            >
              Latest run
            </button>
          </TableHead>
          {RESULT_COLUMNS.map(header)}
          <TableHead className="text-right">t vs bar</TableHead>
          <TableHead className="text-right">vs random entry</TableHead>
          {header(OOS_COLUMN)}
          <TableHead className="hidden 2xl:table-cell">Trend</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {ideas.map((idea) => (
          <TableRow
            key={idea.id}
            className="cursor-pointer"
            onClick={() => navigate(`/backtest-results/${idea.id}`)}
          >
            <TableCell className="whitespace-normal min-w-[14rem]">
              <Link
                to={`/backtest-results/${idea.id}`}
                className="font-medium hover:underline"
                onClick={(e) => e.stopPropagation()}
              >
                {idea.id}
              </Link>
              <div className="text-sm text-muted-foreground max-w-xs truncate">{idea.title}</div>
              <ClassificationLine classification={idea.classification} />
              <SecondaryLine idea={idea} />
            </TableCell>
            <TableCell>
              <StatusBadge status={idea.status} />
            </TableCell>
            <TableCell>
              <VerdictBadge verdict={ideaVerdict(idea)} />
            </TableCell>
            <TableCell className="text-sm whitespace-normal max-w-[10rem]">
              {idea.rejection_basis ?? <span className="text-muted-foreground">n/a</span>}
            </TableCell>
            <ResultCells idea={idea} />
            <TableCell className="hidden 2xl:table-cell">
              {idea.latest_run ? <Sparkline values={idea.latest_run.sparkline} /> : null}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}
