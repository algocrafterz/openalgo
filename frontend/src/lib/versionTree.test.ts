import { describe, expect, it } from 'vitest'
import type { StrategyVersion } from '@/api/backtestResults'
import { makeRun } from '@/test/backtestResultsFixtures'
import {
  buildVersionTree,
  latestVersion,
  NO_VERSION,
  runsByVersion,
  type VersionNode,
} from './versionTree'

const v = (version: string, parent: string | null, note = ''): StrategyVersion => ({
  version,
  strategy_fingerprint: `fp-${version}`,
  parent,
  created_ist: '2026-09-01T10:00:00+05:30',
  note,
})

const labels = (nodes: VersionNode[]): string[] => nodes.map((n) => n.version.version)

describe('buildVersionTree', () => {
  it('returns an empty tree for no versions', () => {
    expect(buildVersionTree([])).toEqual([])
  })

  it('builds a linear chain', () => {
    const tree = buildVersionTree([v('v1', null), v('v2', 'v1'), v('v3', 'v2')])
    expect(labels(tree)).toEqual(['v1'])
    expect(labels(tree[0].children)).toEqual(['v2'])
    expect(labels(tree[0].children[0].children)).toEqual(['v3'])
  })

  it('treats a branch as two children in creation order', () => {
    const tree = buildVersionTree([v('v1', null), v('v2', 'v1'), v('v3a', 'v2'), v('v3b', 'v2')])
    expect(labels(tree[0].children[0].children)).toEqual(['v3a', 'v3b'])
  })

  it('makes a version with an unknown parent a root', () => {
    const tree = buildVersionTree([v('v1', null), v('v5', 'v4')])
    expect(labels(tree)).toEqual(['v1', 'v5'])
  })

  it('does not hang on a cycle and still lists every version once', () => {
    const tree = buildVersionTree([v('v1', 'v2'), v('v2', 'v1')])
    const all: string[] = []
    const walk = (ns: VersionNode[]) => {
      for (const n of ns) {
        all.push(n.version.version)
        walk(n.children)
      }
    }
    walk(tree)
    expect(all.sort()).toEqual(['v1', 'v2'])
  })

  it('treats a self parent as a root', () => {
    expect(labels(buildVersionTree([v('v1', 'v1')]))).toEqual(['v1'])
  })

  it('keeps only the first of duplicate labels', () => {
    const tree = buildVersionTree([v('v1', null, 'first'), v('v1', null, 'second')])
    expect(tree).toHaveLength(1)
    expect(tree[0].version.note).toBe('first')
  })

  it('records depth', () => {
    const tree = buildVersionTree([v('v1', null), v('v2', 'v1')])
    expect(tree[0].depth).toBe(0)
    expect(tree[0].children[0].depth).toBe(1)
  })
})

describe('runsByVersion', () => {
  it('groups by strategy_version keeping run order', () => {
    const a = makeRun({ run_id: 'a', strategy_version: 'v1' })
    const b = makeRun({ run_id: 'b', strategy_version: 'v2' })
    const c = makeRun({ run_id: 'c', strategy_version: 'v1' })
    const groups = runsByVersion([a, b, c])
    expect(groups.get('v1')?.map((r) => r.run_id)).toEqual(['a', 'c'])
    expect(groups.get('v2')?.map((r) => r.run_id)).toEqual(['b'])
  })

  it('puts runs without a version in the not recorded group', () => {
    const a = makeRun({ run_id: 'a' })
    const groups = runsByVersion([a])
    expect(groups.get(NO_VERSION)?.map((r) => r.run_id)).toEqual(['a'])
    expect(NO_VERSION).toBe('Version not recorded')
  })
})

describe('latestVersion', () => {
  it('is the version of the most recent run', () => {
    const runs = [
      makeRun({ run_id: 'a', strategy_version: 'v1' }),
      makeRun({ run_id: 'b', strategy_version: 'v3b' }),
    ]
    expect(latestVersion(runs)).toBe('v3b')
  })

  it('is null with no runs or no recorded version', () => {
    expect(latestVersion([])).toBeNull()
    expect(latestVersion([makeRun({ run_id: 'a' })])).toBeNull()
  })
})

describe('latestVersion by creation order', () => {
  it('uses the highest run sequence number, not the date', () => {
    const runs = [
      makeRun({ run_id: '2026-09-10-x-v1', date: '2026-09-10', strategy_version: 'v1' }),
      makeRun({ run_id: '2026-09-12-x-v2', date: '2026-09-12', strategy_version: 'v2' }),
      makeRun({ run_id: '2026-09-05-x-v3', date: '2026-09-05', strategy_version: 'v3' }),
    ]
    expect(latestVersion(runs)).toBe('v3')
    expect(latestVersion([...runs].reverse())).toBe('v3')
  })

  it('breaks a sequence tie by date', () => {
    const runs = [
      makeRun({ run_id: 'a-v2', date: '2026-09-12', strategy_version: 'v2' }),
      makeRun({ run_id: 'b-v2', date: '2026-09-10', strategy_version: 'v1' }),
    ]
    expect(latestVersion(runs)).toBe('v2')
  })
})
