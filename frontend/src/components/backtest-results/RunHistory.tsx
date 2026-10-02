import type { RunRecord, StrategyVersion, VersionPointers } from '@/api/backtestResults'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { metricText, netClass } from '@/lib/backtestResults'
import { cn } from '@/lib/utils'
import { latestVersion, NO_VERSION, runsByVersion } from '@/lib/versionTree'
import { VersionBadges } from './VersionTree'

interface RunHistoryProps {
  runs: RunRecord[]
  selectedId: string
  onSelect: (runId: string) => void
  /** Frozen versions in creation order. Null or absent: a legacy idea, shown as a flat table. */
  versions?: StrategyVersion[] | null
  pointers?: VersionPointers
}

interface Group {
  label: string
  note: string
  runs: RunRecord[]
}

/** Version creation order (newest version last), then runs whose version is unknown. */
function groupRuns(runs: RunRecord[], versions: StrategyVersion[]): Group[] {
  const byVersion = runsByVersion(runs)
  const groups: Group[] = []
  const used = new Set<string>()
  for (const ver of versions) {
    const list = byVersion.get(ver.version)
    if (!list || used.has(ver.version)) continue
    used.add(ver.version)
    groups.push({ label: ver.version, note: ver.note, runs: list })
  }
  for (const [label, list] of byVersion) {
    if (!used.has(label) && label !== NO_VERSION) groups.push({ label, note: '', runs: list })
  }
  const unrecorded = byVersion.get(NO_VERSION)
  if (unrecorded) groups.push({ label: NO_VERSION, note: '', runs: unrecorded })
  return groups
}

function RunRow({
  run,
  selectedId,
  onSelect,
}: {
  run: RunRecord
  selectedId: string
  onSelect: (runId: string) => void
}) {
  const selected = run.run_id === selectedId
  return (
    <TableRow data-state={selected ? 'selected' : undefined}>
      <TableCell>
        <button
          type="button"
          className={cn('font-mono underline-offset-4 hover:underline', selected && 'font-bold')}
          aria-current={selected ? 'true' : undefined}
          onClick={() => onSelect(run.run_id)}
        >
          {run.date}
        </button>
      </TableCell>
      <TableCell className="font-mono text-xs">
        {run.run_id}
        {run.promoted && <span className="ml-2 font-sans">(promoted to forward test)</span>}
      </TableCell>
      <TableCell className={cn('text-right font-mono', netClass(run.metrics))}>
        {metricText(run.metrics, run.metrics.net_bps)}
      </TableCell>
      <TableCell className="text-right font-mono">{run.metrics.trades}</TableCell>
      <TableCell className="text-right font-mono">{run.n_trials ?? 1}</TableCell>
      <TableCell className="font-mono text-xs">{run.parent_run_id ?? 'first run'}</TableCell>
      <TableCell className="text-sm whitespace-normal break-words min-w-[18rem]">
        {run.change_note || 'n/a'}
      </TableCell>
    </TableRow>
  )
}

function GroupRows({
  group,
  pointers,
  latest,
  selectedId,
  onSelect,
}: {
  group: Group
  pointers?: VersionPointers
  latest: string | null
  selectedId: string
  onSelect: (runId: string) => void
}) {
  const ordered = [...group.runs].reverse()
  return (
    <>
      <TableRow data-testid="version-group" className="bg-muted/40">
        <TableCell colSpan={7}>
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="font-mono font-semibold">{group.label}</h3>
            <VersionBadges label={group.label} pointers={pointers} latest={latest} />
            {group.note && <span className="text-sm whitespace-normal">{group.note}</span>}
          </div>
        </TableCell>
      </TableRow>
      {ordered.map((run) => (
        <RunRow key={run.run_id} run={run} selectedId={selectedId} onSelect={onSelect} />
      ))}
    </>
  )
}

/**
 * Every recorded run. Flat: newest first. With versions: grouped by version in creation order
 * (newest version last), newest run first inside each group.
 */
export function RunHistory({ runs, selectedId, onSelect, versions, pointers }: RunHistoryProps) {
  const ordered = [...runs].reverse()
  const groups = versions && versions.length > 0 ? groupRuns(runs, versions) : null
  const latest = latestVersion(runs)
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <h2>Run history ({runs.length})</h2>
        </CardTitle>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Date</TableHead>
              <TableHead>Run</TableHead>
              <TableHead className="text-right">Net bps</TableHead>
              <TableHead className="text-right">Trades</TableHead>
              <TableHead className="text-right">Variants tried</TableHead>
              <TableHead>Changed from</TableHead>
              <TableHead>What changed</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {groups
              ? groups.map((g) => (
                  <GroupRows
                    key={g.label}
                    group={g}
                    pointers={pointers}
                    latest={latest}
                    selectedId={selectedId}
                    onSelect={onSelect}
                  />
                ))
              : ordered.map((run) => (
                  <RunRow key={run.run_id} run={run} selectedId={selectedId} onSelect={onSelect} />
                ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  )
}
