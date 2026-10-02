import type {
  RunRecord,
  RunVerdictValue,
  StrategyVersion,
  Verdict,
  VersionPointers,
} from '@/api/backtestResults'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'
import { buildVersionTree, latestVersion, NO_VERSION, type VersionNode } from '@/lib/versionTree'
import { VerdictBadge } from './VerdictBadge'

const CALCULATED_TO_VERDICT: Record<RunVerdictValue, Verdict> = {
  EDGE: 'edge',
  MARGINAL: 'marginal',
  NO_EDGE: 'no-edge',
  INCONCLUSIVE: 'inconclusive',
}

/** Stored created_ist is already IST; show it as a date and time with the zone named. */
export function formatCreatedIst(value: string | undefined): string {
  if (!value) return 'date not recorded'
  const m = value.match(/^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}))?/)
  if (!m) return value
  return m[2] ? `${m[1]} ${m[2]} IST` : `${m[1]} IST`
}

/** True for a plain v<number> label; anything else (v3b) is a branch. */
export function isBranchLabel(label: string): boolean {
  return !/^v\d+$/.test(label)
}

interface VersionTreeProps {
  versions: StrategyVersion[] | null
  /** A plain sentence when versions.yaml exists but could not be read. */
  versionsError?: string | null
  pointers?: VersionPointers
  runs: RunRecord[]
  selectedRunId?: string
  onSelectRun?: (id: string) => void
}

function RunRow({
  run,
  selected,
  onSelectRun,
}: {
  run: RunRecord
  selected: boolean
  onSelectRun?: (id: string) => void
}) {
  const classes = run.change_classes ?? []
  return (
    <li className="flex flex-wrap items-center gap-2 py-0.5 text-xs" data-testid="version-run">
      <button
        type="button"
        className={cn('font-mono underline-offset-4 hover:underline', selected && 'font-bold')}
        aria-current={selected ? 'true' : undefined}
        onClick={() => onSelectRun?.(run.run_id)}
      >
        {run.run_id}
      </button>
      <span className="text-muted-foreground">{run.date}</span>
      {run.run_verdict ? (
        <span data-testid="version-run-verdict">
          <VerdictBadge verdict={CALCULATED_TO_VERDICT[run.run_verdict] ?? 'none'} />
        </span>
      ) : (
        <span className="text-muted-foreground">verdict not calculated</span>
      )}
      {classes.map((c) => (
        <Badge key={c} variant="secondary" className="text-[10px]" data-testid="version-run-class">
          {c}
        </Badge>
      ))}
      {run.reproducible === false && (
        <span className="text-amber-700" data-testid="version-run-not-reproducible">
          not reproducible
        </span>
      )}
    </li>
  )
}

function RunList({
  runs,
  selectedRunId,
  onSelectRun,
}: {
  runs: RunRecord[]
  selectedRunId?: string
  onSelectRun?: (id: string) => void
}) {
  if (runs.length === 0) {
    return <p className="py-0.5 text-xs text-muted-foreground">No runs on this version yet</p>
  }
  return (
    <ul className="space-y-0.5">
      {runs.map((run) => (
        <RunRow
          key={run.run_id}
          run={run}
          selected={run.run_id === selectedRunId}
          onSelectRun={onSelectRun}
        />
      ))}
    </ul>
  )
}

interface NodeCtx {
  pointers?: VersionPointers
  latest: string | null
  grouped: Map<string, RunRecord[]>
  selectedRunId?: string
  onSelectRun?: (id: string) => void
}

export function VersionBadges({
  label,
  pointers,
  latest,
}: {
  label: string
  pointers?: VersionPointers
  latest: string | null
}) {
  return (
    <>
      {pointers?.current_version === label && (
        <Badge data-testid="badge-current" className="text-[10px]">
          CURRENT
        </Badge>
      )}
      {latest === label && (
        <Badge variant="outline" data-testid="badge-latest" className="text-[10px]">
          LATEST
        </Badge>
      )}
      {pointers?.promoted_version === label && (
        <Badge variant="secondary" data-testid="badge-promoted" className="text-[10px]">
          PROMOTED
          {pointers.promoted_run_id ? ` (${pointers.promoted_run_id})` : ''}
        </Badge>
      )}
    </>
  )
}

function Node({ node, ctx, last }: { node: VersionNode; ctx: NodeCtx; last: boolean }) {
  const ver = node.version
  return (
    <li data-testid="version-node" className={cn('relative', node.depth > 0 && 'pl-5')}>
      {node.depth > 0 && (
        <span
          aria-hidden="true"
          className="absolute left-0 top-0 font-mono text-muted-foreground select-none"
        >
          {last ? '`--' : '|--'}
        </span>
      )}
      <div data-version={ver.version}>
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="font-mono font-semibold">{ver.version}</h3>
          {isBranchLabel(ver.version) && (
            <Badge variant="outline" className="text-[10px]" data-testid="branch-label">
              branch {ver.version}
            </Badge>
          )}
          <VersionBadges label={ver.version} pointers={ctx.pointers} latest={ctx.latest} />
          <span className="text-xs text-muted-foreground">{formatCreatedIst(ver.created_ist)}</span>
          {ver.parent && <span className="text-xs text-muted-foreground">from {ver.parent}</span>}
        </div>
        {ver.note && <p className="text-xs">{ver.note}</p>}
        <div className="pl-4">
          <RunList
            runs={ctx.grouped.get(ver.version) ?? []}
            selectedRunId={ctx.selectedRunId}
            onSelectRun={ctx.onSelectRun}
          />
        </div>
      </div>
      {node.children.length > 0 && (
        <ul className="mt-1 space-y-2 border-l border-border ml-1">
          {node.children.map((child, i) => (
            <Node
              key={child.version.version}
              node={child}
              ctx={ctx}
              last={i === node.children.length - 1}
            />
          ))}
        </ul>
      )}
    </li>
  )
}

function Extra({ label, runs, ctx }: { label: string; runs: RunRecord[]; ctx: NodeCtx }) {
  return (
    <li data-testid="version-node">
      <div data-version={label}>
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="font-semibold">{label}</h3>
          <VersionBadges label={label} pointers={ctx.pointers} latest={ctx.latest} />
        </div>
        <div className="pl-4">
          <RunList runs={runs} selectedRunId={ctx.selectedRunId} onSelectRun={ctx.onSelectRun} />
        </div>
      </div>
    </li>
  )
}

function PointerWarnings({ pointers, known }: { pointers?: VersionPointers; known: Set<string> }) {
  const names = [pointers?.current_version, pointers?.promoted_version].filter(
    (n, i, all): n is string => Boolean(n) && !known.has(n as string) && all.indexOf(n) === i
  )
  if (names.length === 0) return null
  return (
    <ul className="space-y-0.5" data-testid="pointer-warnings">
      {names.map((n) => (
        <li key={n} className="text-xs text-amber-700">
          {`Pointer to unknown version ${n}`}
        </li>
      ))}
    </ul>
  )
}

function VersionsErrorNotice({ message }: { message?: string | null }) {
  if (!message) return null
  return (
    <output className="block text-sm text-amber-700" data-testid="versions-error">
      {message}
    </output>
  )
}

/** Version lineage with the runs of each version beneath it. Pure presentation of loaded data. */
export function VersionTree({
  versions,
  versionsError,
  pointers,
  runs,
  selectedRunId,
  onSelectRun,
}: VersionTreeProps) {
  if (versions === null) {
    return (
      <section
        aria-labelledby="version-tree-title"
        className="space-y-2"
        data-testid="version-tree"
      >
        <h2 id="version-tree-title" className="text-lg font-semibold">
          Version history
        </h2>
        <VersionsErrorNotice message={versionsError} />
        {!versionsError && (
          <p className="text-sm text-muted-foreground">No version history recorded for this idea</p>
        )}
        <ul className="space-y-0.5" data-testid="flat-runs">
          {runs.map((run) => (
            <RunRow
              key={run.run_id}
              run={run}
              selected={run.run_id === selectedRunId}
              onSelectRun={onSelectRun}
            />
          ))}
        </ul>
      </section>
    )
  }

  const tree = buildVersionTree(versions)
  const known = new Set(versions.map((v) => v.version))
  const grouped = new Map<string, RunRecord[]>()
  for (const run of runs) {
    const key = run.strategy_version || NO_VERSION
    grouped.set(key, [...(grouped.get(key) ?? []), run])
  }
  const extras = [...grouped.entries()].filter(([key]) => !known.has(key))
  const ctx: NodeCtx = {
    pointers,
    latest: latestVersion(runs),
    grouped,
    selectedRunId,
    onSelectRun,
  }

  return (
    <section aria-labelledby="version-tree-title" className="space-y-2" data-testid="version-tree">
      <h2 id="version-tree-title" className="text-lg font-semibold">
        Version history
      </h2>
      <VersionsErrorNotice message={versionsError} />
      <PointerWarnings pointers={pointers} known={known} />
      <ul
        aria-labelledby="version-tree-title"
        className="space-y-2 text-sm"
        data-testid="version-list"
      >
        {tree.map((node, i) => (
          <Node key={node.version.version} node={node} ctx={ctx} last={i === tree.length - 1} />
        ))}
        {extras.map(([label, list]) => (
          <Extra key={label} label={label} runs={list} ctx={ctx} />
        ))}
      </ul>
    </section>
  )
}
