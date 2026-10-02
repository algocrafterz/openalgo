import type { RunRecord, StrategyVersion } from '@/api/backtestResults'

export const NO_VERSION = 'Version not recorded'

export interface VersionNode {
  version: StrategyVersion
  depth: number
  children: VersionNode[]
}

/**
 * Presentation only: reshape the frozen versions into a tree through their parent links.
 * Roots are versions with no parent or an unknown one. Children keep creation order. Duplicate
 * labels keep the first; a parent cycle is broken by promoting its first member to a root.
 */
export function buildVersionTree(versions: StrategyVersion[]): VersionNode[] {
  const unique: StrategyVersion[] = []
  const seen = new Set<string>()
  for (const ver of versions) {
    if (seen.has(ver.version)) continue
    seen.add(ver.version)
    unique.push(ver)
  }

  const childrenOf = new Map<string, StrategyVersion[]>()
  const roots: StrategyVersion[] = []
  for (const ver of unique) {
    if (ver.parent && ver.parent !== ver.version && seen.has(ver.parent)) {
      childrenOf.set(ver.parent, [...(childrenOf.get(ver.parent) ?? []), ver])
    } else {
      roots.push(ver)
    }
  }

  const placed = new Set<string>()
  const build = (ver: StrategyVersion, depth: number): VersionNode => {
    placed.add(ver.version)
    const kids = (childrenOf.get(ver.version) ?? []).filter((c) => !placed.has(c.version))
    return { version: ver, depth, children: kids.map((c) => build(c, depth + 1)) }
  }

  const tree = roots.map((r) => build(r, 0))
  // Anything unreachable from a root sits on a cycle: surface it rather than drop it.
  for (const ver of unique) {
    if (!placed.has(ver.version)) tree.push(build(ver, 0))
  }
  return tree
}

/** Runs grouped by the version that produced them, in the order given. */
export function runsByVersion(runs: RunRecord[]): Map<string, RunRecord[]> {
  const groups = new Map<string, RunRecord[]>()
  for (const run of runs) {
    const key = run.strategy_version || NO_VERSION
    groups.set(key, [...(groups.get(key) ?? []), run])
  }
  return groups
}

/** Per-idea creation number carried by a run id (`<date>-<slug>-v<N>`), or -1 when absent. */
function runSequence(runId: string): number {
  const m = runId.match(/-v(\d+)$/)
  return m ? Number.parseInt(m[1], 10) : -1
}

/**
 * The version of the latest run: the highest sequence number in the run id (creation order, so a
 * back-dated run created later wins), ties broken by date, then by position.
 */
export function latestVersion(runs: RunRecord[]): string | null {
  let best: RunRecord | null = null
  for (const run of runs) {
    if (!best) {
      best = run
      continue
    }
    const [a, b] = [runSequence(run.run_id), runSequence(best.run_id)]
    if (a > b || (a === b && run.date >= best.date)) best = run
  }
  return best ? (best.strategy_version ?? null) : null
}
