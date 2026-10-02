import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { StrategyVersion } from '@/api/backtestResults'
import { makeRun } from '@/test/backtestResultsFixtures'
import { RunHistory } from './RunHistory'

const v = (version: string, parent: string | null, note = ''): StrategyVersion => ({
  version,
  strategy_fingerprint: `fp-${version}`,
  parent,
  created_ist: '2026-09-01T10:30:00+05:30',
  note,
})

const RUNS = [
  makeRun({ run_id: 'r1', date: '2026-09-01', strategy_version: 'v1' }),
  makeRun({ run_id: 'r2', date: '2026-09-02', strategy_version: 'v1', change_note: 'tune' }),
  makeRun({ run_id: 'r3', date: '2026-09-03', strategy_version: 'v2' }),
]
const VERSIONS = [v('v1', null, 'first cut'), v('v2', 'v1', 'tighter stop')]

describe('RunHistory flat fallback', () => {
  it('renders the flat table newest first when there are no versions', () => {
    render(<RunHistory runs={RUNS} selectedId="r1" onSelect={vi.fn()} />)
    expect(screen.getByText('Run history (3)')).toBeInTheDocument()
    expect(screen.queryByTestId('version-group')).not.toBeInTheDocument()
    const ids = screen
      .getAllByRole('row')
      .slice(1)
      .map((r) => r.textContent ?? '')
    expect(ids[0]).toContain('r3')
    expect(ids[2]).toContain('r1')
  })

  it('also falls back when versions is null', () => {
    render(<RunHistory runs={RUNS} selectedId="r1" onSelect={vi.fn()} versions={null} />)
    expect(screen.queryByTestId('version-group')).not.toBeInTheDocument()
  })

  it('selects a run by its date button and marks the selected one', async () => {
    const onSelect = vi.fn()
    render(<RunHistory runs={RUNS} selectedId="r1" onSelect={onSelect} />)
    await userEvent.click(screen.getByRole('button', { name: '2026-09-02' }))
    expect(onSelect).toHaveBeenCalledWith('r2')
    expect(screen.getByRole('button', { name: '2026-09-01' })).toHaveAttribute(
      'aria-current',
      'true'
    )
  })
})

describe('RunHistory grouped by version', () => {
  it('groups in version creation order, newest version last, newest run first inside', () => {
    render(<RunHistory runs={RUNS} selectedId="r1" onSelect={vi.fn()} versions={VERSIONS} />)
    const groups = screen.getAllByTestId('version-group')
    expect(groups.map((g) => g.textContent?.slice(0, 2))).toEqual(['v1', 'v2'])
    const rows = screen.getAllByRole('row').map((r) => r.textContent ?? '')
    const idx = (id: string) => rows.findIndex((t) => t.includes(id) && !t.startsWith('v'))
    expect(idx('r2')).toBeLessThan(idx('r1'))
  })

  it('shows the note and separate badges in each group header', () => {
    render(
      <RunHistory
        runs={RUNS}
        selectedId="r1"
        onSelect={vi.fn()}
        versions={VERSIONS}
        pointers={{ current_version: 'v2', promoted_version: 'v1', promoted_run_id: 'r1' }}
      />
    )
    const [g1, g2] = screen.getAllByTestId('version-group')
    expect(within(g1).getByText('first cut')).toBeInTheDocument()
    expect(within(g1).getByTestId('badge-promoted')).toBeInTheDocument()
    expect(within(g1).queryByTestId('badge-current')).not.toBeInTheDocument()
    expect(within(g2).getByText('tighter stop')).toBeInTheDocument()
    expect(within(g2).getByTestId('badge-current')).toBeInTheDocument()
    expect(within(g2).getByTestId('badge-latest')).toBeInTheDocument()
  })

  it('keeps selection working inside groups', async () => {
    const onSelect = vi.fn()
    render(<RunHistory runs={RUNS} selectedId="r3" onSelect={onSelect} versions={VERSIONS} />)
    await userEvent.click(screen.getByRole('button', { name: '2026-09-01' }))
    expect(onSelect).toHaveBeenCalledWith('r1')
    expect(screen.getByRole('button', { name: '2026-09-03' })).toHaveAttribute(
      'aria-current',
      'true'
    )
  })

  it('puts runs with no recorded version in a trailing group', () => {
    const legacy = makeRun({ run_id: 'old', date: '2026-08-01' })
    render(
      <RunHistory runs={[legacy, ...RUNS]} selectedId="r1" onSelect={vi.fn()} versions={VERSIONS} />
    )
    const groups = screen.getAllByTestId('version-group')
    expect(groups[groups.length - 1]).toHaveTextContent('Version not recorded')
  })

  it('omits versions that have no runs', () => {
    render(
      <RunHistory
        runs={RUNS}
        selectedId="r1"
        onSelect={vi.fn()}
        versions={[...VERSIONS, v('v3', 'v2', 'no runs yet')]}
      />
    )
    expect(screen.getAllByTestId('version-group')).toHaveLength(2)
  })

  it('has an h2 section heading', () => {
    render(<RunHistory runs={RUNS} selectedId="r1" onSelect={vi.fn()} />)
    expect(screen.getByRole('heading', { level: 2, name: 'Run history (3)' })).toBeInTheDocument()
  })
})
