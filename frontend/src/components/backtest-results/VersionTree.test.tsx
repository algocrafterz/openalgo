import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { StrategyVersion } from '@/api/backtestResults'
import { makeRun } from '@/test/backtestResultsFixtures'
import { VersionTree } from './VersionTree'

const v = (version: string, parent: string | null, note = ''): StrategyVersion => ({
  version,
  strategy_fingerprint: `fp-${version}`,
  parent,
  created_ist: '2026-09-01T10:30:00+05:30',
  note,
})

const VERSIONS = [
  v('v1', null, 'first cut'),
  v('v2', 'v1', 'tighter stop'),
  v('v3b', 'v2', 'wider entry'),
]

const RUNS = [
  makeRun({
    run_id: 'r1',
    strategy_version: 'v1',
    run_verdict: 'NO_EDGE',
    change_classes: ['parameter_change'],
  }),
  makeRun({ run_id: 'r2', strategy_version: 'v2', run_verdict: 'EDGE', reproducible: false }),
  makeRun({ run_id: 'r3', strategy_version: 'v3b', run_verdict: 'MARGINAL' }),
]

const node = (label: string) => document.querySelector(`[data-version="${label}"]`) as HTMLElement

describe('VersionTree', () => {
  it('renders a labelled nested list with a heading per version', () => {
    render(<VersionTree versions={VERSIONS} runs={RUNS} />)
    expect(screen.getByRole('heading', { name: 'Version history' })).toBeInTheDocument()
    expect(screen.getByRole('list', { name: 'Version history' })).toBeInTheDocument()
    expect(screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent)).toEqual([
      'v1',
      'v2',
      'v3b',
    ])
    expect(screen.getAllByTestId('version-node')).toHaveLength(3)
  })

  it('shows note, IST created date and branch label', () => {
    render(<VersionTree versions={VERSIONS} runs={RUNS} />)
    expect(screen.getByText('tighter stop')).toBeInTheDocument()
    expect(screen.getAllByText('2026-09-01 10:30 IST').length).toBe(3)
    expect(screen.getAllByTestId('branch-label')).toHaveLength(1)
    expect(screen.getByTestId('branch-label')).toHaveTextContent('v3b')
  })

  it('renders CURRENT, LATEST and PROMOTED separately on different versions', () => {
    render(
      <VersionTree
        versions={VERSIONS}
        runs={RUNS}
        pointers={{ current_version: 'v3b', promoted_version: 'v1', promoted_run_id: 'r1' }}
      />
    )
    const v1 = node('v1')
    const v2 = node('v2')
    const v3b = node('v3b')
    expect(within(v3b).getByTestId('badge-current')).toBeInTheDocument()
    expect(within(v3b).getByTestId('badge-latest')).toBeInTheDocument()
    expect(within(v1).getByTestId('badge-promoted')).toHaveTextContent('PROMOTED (r1)')
    expect(within(v1).queryByTestId('badge-current')).not.toBeInTheDocument()
    expect(within(v2).queryByTestId('badge-current')).not.toBeInTheDocument()
    expect(within(v2).queryByTestId('badge-promoted')).not.toBeInTheDocument()
    expect(screen.getAllByTestId('badge-current')).toHaveLength(1)
    expect(screen.getAllByTestId('badge-latest')).toHaveLength(1)
    expect(screen.getAllByTestId('badge-promoted')).toHaveLength(1)
  })

  it('puts each run under its own version with verdict, change classes and flag', () => {
    render(<VersionTree versions={VERSIONS} runs={RUNS} />)
    const v1 = node('v1')
    expect(within(v1).getByText('r1')).toBeInTheDocument()
    expect(within(v1).getByText('no edge')).toBeInTheDocument()
    expect(within(v1).getByText('parameter_change')).toBeInTheDocument()
    expect(within(v1).queryByText('not reproducible')).not.toBeInTheDocument()
    const v2 = node('v2')
    expect(within(v2).getByText('edge')).toBeInTheDocument()
    expect(within(v2).getByText('not reproducible')).toBeInTheDocument()
  })

  it('calls onSelectRun when a run is clicked and marks the selected run', async () => {
    const onSelectRun = vi.fn()
    render(
      <VersionTree versions={VERSIONS} runs={RUNS} selectedRunId="r2" onSelectRun={onSelectRun} />
    )
    await userEvent.click(screen.getByRole('button', { name: 'r3' }))
    expect(onSelectRun).toHaveBeenCalledWith('r3')
    expect(screen.getByRole('button', { name: 'r2' })).toHaveAttribute('aria-current', 'true')
  })

  it('run buttons are keyboard operable', async () => {
    const onSelectRun = vi.fn()
    render(<VersionTree versions={VERSIONS} runs={RUNS} onSelectRun={onSelectRun} />)
    screen.getByRole('button', { name: 'r1' }).focus()
    await userEvent.keyboard('{Enter}')
    expect(onSelectRun).toHaveBeenCalledWith('r1')
  })

  it('shows a plain note and a flat run list when versions is null', () => {
    render(<VersionTree versions={null} runs={RUNS} />)
    expect(screen.getByText('No version history recorded for this idea')).toBeInTheDocument()
    expect(screen.queryByTestId('version-list')).not.toBeInTheDocument()
    expect(within(screen.getByTestId('flat-runs')).getAllByTestId('version-run')).toHaveLength(3)
  })

  it('groups runs without a version under a not recorded entry', () => {
    render(<VersionTree versions={VERSIONS} runs={[...RUNS, makeRun({ run_id: 'old' })]} />)
    const extra = node('Version not recorded')
    expect(within(extra).getByText('old')).toBeInTheDocument()
  })

  it('says so when a version has no runs', () => {
    render(<VersionTree versions={[v('v1', null)]} runs={[]} />)
    expect(screen.getByText('No runs on this version yet')).toBeInTheDocument()
  })

  it('warns when a pointer names a version that is not in the list', () => {
    render(
      <VersionTree
        versions={VERSIONS}
        runs={RUNS}
        pointers={{ current_version: 'v9', promoted_version: 'v8', promoted_run_id: null }}
      />
    )
    expect(screen.getByText('Pointer to unknown version v9')).toBeInTheDocument()
    expect(screen.getByText('Pointer to unknown version v8')).toBeInTheDocument()
    expect(screen.queryByTestId('badge-current')).toBeNull()
  })

  it('does not warn when the pointers name known versions or are empty', () => {
    render(
      <VersionTree
        versions={VERSIONS}
        runs={RUNS}
        pointers={{ current_version: 'v2', promoted_version: null, promoted_run_id: null }}
      />
    )
    expect(screen.queryByText(/Pointer to unknown version/)).toBeNull()
  })

  it('shows versions_error as a notice and not the plain no-history note', () => {
    render(<VersionTree versions={null} versionsError="versions.yaml is unreadable" runs={RUNS} />)
    expect(screen.getByRole('status')).toHaveTextContent('versions.yaml is unreadable')
    expect(screen.queryByText('No version history recorded for this idea')).toBeNull()
    expect(screen.getAllByTestId('version-run')).toHaveLength(3)
  })

  it('shows the plain note when versions is null without an error', () => {
    render(<VersionTree versions={null} versionsError={null} runs={RUNS} />)
    expect(screen.getByText('No version history recorded for this idea')).toBeInTheDocument()
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('shows the error notice alongside a tree that did load', () => {
    render(<VersionTree versions={VERSIONS} versionsError="partial read" runs={RUNS} />)
    expect(screen.getByRole('status')).toHaveTextContent('partial read')
    expect(screen.getAllByTestId('version-node')).toHaveLength(3)
  })

  it('marks LATEST by creation order, not by date', () => {
    const runs = [
      makeRun({ run_id: 'd-v1', date: '2026-09-10', strategy_version: 'v1' }),
      makeRun({ run_id: 'd-v2', date: '2026-09-01', strategy_version: 'v2' }),
    ]
    render(<VersionTree versions={VERSIONS} runs={runs} />)
    expect(within(node('v2')).getByTestId('badge-latest')).toBeInTheDocument()
    expect(within(node('v1')).queryByTestId('badge-latest')).toBeNull()
  })
})
