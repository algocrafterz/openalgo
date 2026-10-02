import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { RunComparison } from '@/api/backtestResults'
import { CompareDetail } from './CompareDetail'

vi.mock('./InfoTip', () => ({ InfoTip: ({ term }: { term: string }) => <span>{term}</span> }))
vi.mock('./LineChart', () => ({
  LineChart: ({
    title,
    series,
  }: {
    title: string
    series: { label: string; points: [string, number][] }[]
  }) => (
    <div data-testid={`chart-${title}`}>
      {series.map((s) => (
        <span key={s.label} data-testid={`pts-${title}-${s.label}`}>
          {s.points.length}
        </span>
      ))}
    </div>
  ),
}))

const NO_SPEC = { changed: {}, added: {}, removed: {} }

function make(over: Partial<RunComparison> = {}): RunComparison {
  return {
    schema: 1,
    change_classes: ['STRATEGY_CHANGE', 'DATA_RANGE_CHANGE'],
    strategy: {
      version: { a: 'v1', b: 'v2' },
      same_version: false,
      rules: { changed: { stop: { a: 'atr1', b: 'atr2' } }, added: { filter_x: 1 }, removed: {} },
      parameters: { changed: { lookback: { a: 10, b: 20 } }, added: {}, removed: { gap: 3 } },
      fingerprints: {
        rules: { a: 'aaaaaaaa1111', b: 'bbbbbbbb2222', changed: true },
        params: { a: 'cccccccc3333', b: 'cccccccc3333', changed: false },
        strategy: { a: 'dddddddd4444', b: 'eeeeeeee5555', changed: true },
      },
    },
    scope: [{ field: 'universe', label: 'Universe', a: 'FNO', b: 'FNO200', changed: true }],
    dataset: {
      fields: [{ field: 'source', label: 'Source', a: 'x', b: 'x', changed: false }],
      fingerprint_changed: true,
    },
    execution: [
      { field: 'entry', label: 'Entry fill', a: 'next_open', b: 'next_open', changed: false },
    ],
    cost: [{ field: 'rt', label: 'Round trip bps', a: 5, b: 8, changed: true }],
    performance: {
      trades: { a: 100, b: 150, delta: 50 },
      expectancy_r: { a: 0.1, b: null, delta: null },
      max_dd_r: { a: -5, b: -7, delta: -2 },
    },
    performance_common: {
      trades: { a: 100, b: 90, delta: -10 },
      net_bps: { a: 2, b: 4, delta: 2 },
    },
    overlap: {
      periods: {
        a: { start: '2024-01-01', end: '2024-06-30' },
        b: { start: '2024-01-01', end: '2024-09-30' },
      },
      common: { start: '2024-01-01', end: '2024-06-30' },
      extra: { a: [], b: [{ start: '2024-07-01', end: '2024-09-30' }] },
      warning: 'Run B has extra sessions 2024-07-01 to 2024-09-30',
    },
    views: {
      full: {
        a: { trades: 100, gross_bps: 1, net_bps: 2 },
        b: { trades: 150, gross_bps: 1, net_bps: 3 },
      },
      common: {
        a: { trades: 100, gross_bps: 1, net_bps: 2 },
        b: { trades: 90, gross_bps: 1, net_bps: 4 },
      },
      incremental: {
        run: 'b',
        ranges: [{ start: '2024-07-01', end: '2024-09-30' }],
        metrics: { trades: 60, gross_bps: 1, net_bps: 9 },
      },
    },
    curves: {
      equity_r: [
        { x: '2024-01-02', a: 0, b: 0 },
        { x: '2024-07-02', a: null, b: 5 },
        { x: '2024-08-02', a: null, b: 6 },
      ],
      drawdown_r: [{ x: '2024-01-02', a: 0, b: 0 }],
      monthly: [
        { x: '2024-01', a: 1, b: 1 },
        { x: '2024-07', a: null, b: 2 },
      ],
      rolling: [{ x: '2024-01-02', a: 0.1, b: 0.2 }],
      rolling_window: 50,
      distribution: [{ bin_start: -1, bin_end: 0, a: 3, b: 4 }],
      common: { start: '2024-01-01', end: '2024-06-30' },
      incremental: [{ start: '2024-07-01', end: '2024-09-30' }],
    },
    curves_common: {
      equity_r: [
        { x: '2024-01-02', a: 0, b: 0 },
        { x: '2024-03-02', a: 1, b: 1 },
        { x: '2024-06-02', a: 2, b: 2 },
        { x: '2024-08-01', a: 3, b: 3 },
      ],
      drawdown_r: [
        { x: '2024-01-02', a: 0, b: 0 },
        { x: '2024-03-02', a: -1, b: -2 },
      ],
      monthly: [{ x: '2024-03', a: 7, b: 8 }],
      rolling: [{ x: '2024-01-02', a: 0.1, b: 0.2 }],
      rolling_window: 50,
      distribution: [{ bin_start: -1, bin_end: 0, a: 3, b: 4 }],
      common: { start: '2024-01-01', end: '2024-06-30' },
      incremental: [],
    },
    curves_incremental: {
      equity_r: [
        { x: '2024-07-02', a: null, b: 0 },
        { x: '2024-08-02', a: null, b: 4 },
      ],
      drawdown_r: [{ x: '2024-07-02', a: null, b: 0 }],
      monthly: [{ x: '2024-08', a: null, b: 5 }],
      rolling: [{ x: '2024-07-02', a: null, b: 0.3 }],
      rolling_window: 50,
      distribution: [{ bin_start: -1, bin_end: 0, a: 0, b: 4 }],
      common: null,
      incremental: [{ start: '2024-07-01', end: '2024-09-30' }],
    },
    ...over,
  }
}

const RUN_A = { run_id: 'BT-1', date: '2024-07-05', code_version: 'abc' }
const RUN_B = { run_id: 'BT-2', date: '2024-10-05', code_version: 'def' }

function show(c: RunComparison) {
  return render(<CompareDetail comparison={c} runA={RUN_A} runB={RUN_B} />)
}

describe('CompareDetail', () => {
  it('renders sections in the fixed order', () => {
    show(make())
    const names = screen.getAllByRole('heading').map((h) => h.textContent)
    const order = ['Strategy', 'Scope', 'Dataset', 'Execution', 'Cost', 'Performance', 'Curves']
    const idx = order.map((o) => names.findIndex((n) => n?.startsWith(o)))
    expect(idx.every((i) => i >= 0)).toBe(true)
    expect([...idx].sort((x, y) => x - y)).toEqual(idx)
    expect(screen.getByText('BT-1')).toBeInTheDocument()
    expect(screen.getByText('BT-2')).toBeInTheDocument()
  })

  it('labels change classes and marks the first as primary', () => {
    show(make())
    const list = screen.getByRole('list', { name: 'Change classes' })
    const items = within(list).getAllByRole('listitem')
    expect(items).toHaveLength(2)
    expect(items[0]).toHaveTextContent('Primary')
    expect(items[0]).toHaveTextContent('STRATEGY_CHANGE')
  })

  it('reads RESEARCH_RERUN as Identical re-run', () => {
    show(make({ change_classes: ['RESEARCH_RERUN'] }))
    expect(screen.getByText('Identical re-run')).toBeInTheDocument()
    expect(screen.queryByText('RESEARCH_RERUN')).not.toBeInTheDocument()
  })

  it('shows the overlap warning only when set and names the span', () => {
    const { unmount } = show(make())
    expect(screen.getByRole('alert')).toHaveTextContent('2024-07-01 to 2024-09-30')
    unmount()
    show(make({ overlap: { ...make().overlap, warning: null } }))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('disables unavailable views with a reason', () => {
    const base = make()
    show(
      make({
        views: { ...base.views, common: null, incremental: null },
        performance_common: null,
      })
    )
    const group = screen.getByRole('radiogroup', { name: 'Period view' })
    expect(within(group).getByRole('radio', { name: /Full period/ })).toBeEnabled()
    expect(within(group).getByRole('radio', { name: /Common period/ })).toBeDisabled()
    expect(within(group).getByRole('radio', { name: /Incremental period/ })).toBeDisabled()
    expect(screen.getByText(/no common sessions/i)).toBeInTheDocument()
    expect(screen.getByText(/no sessions outside the common period/i)).toBeInTheDocument()
  })

  it('switching view changes the performance table and the curves', () => {
    show(make())
    const perf = screen.getByRole('table', { name: 'Performance metrics' })
    expect(within(perf).getByText('150')).toBeInTheDocument()
    expect(screen.getByTestId('pts-Equity (R)-Run B')).toHaveTextContent('3')

    fireEvent.click(screen.getByRole('radio', { name: /Common period/ }))
    expect(
      within(screen.getByRole('table', { name: 'Performance metrics' })).getByText('90')
    ).toBeInTheDocument()
    expect(screen.getByTestId('pts-Equity (R)-Run B')).toHaveTextContent('4')

    fireEvent.click(screen.getByRole('radio', { name: /Incremental period/ }))
    const inc = screen.getByRole('table', { name: 'Performance metrics' })
    expect(within(inc).getByText('60')).toBeInTheDocument()
    expect(
      screen.getByText(/2024-07-01 to 2024-09-30/, {
        selector: '[data-testid="incremental-range"]',
      })
    ).toBeInTheDocument()
    expect(screen.getByTestId('pts-Equity (R)-Run B')).toHaveTextContent('2')
    expect(screen.queryByTestId('pts-Equity (R)-Run A')).not.toBeInTheDocument()
  })

  it('common view shows the backend deltas for the common sessions', () => {
    show(make())
    fireEvent.click(screen.getByRole('radio', { name: /Common period/ }))
    const perf = screen.getByRole('table', { name: 'Performance metrics' })
    expect(perf.querySelector('[data-delta="trades"]')).toHaveTextContent('-10')
    expect(perf.querySelector('[data-delta="net_bps"]')).toHaveTextContent('+2')
    expect(perf.querySelector('[data-delta="trades"]')).not.toHaveTextContent('50')
    expect(screen.queryByText(/full period only/i)).not.toBeInTheDocument()
  })

  it('full view still shows the full-period deltas', () => {
    show(make())
    const perf = screen.getByRole('table', { name: 'Performance metrics' })
    expect(perf.querySelector('[data-delta="trades"]')).toHaveTextContent('+50')
    fireEvent.click(screen.getByRole('radio', { name: /Common period/ }))
    fireEvent.click(screen.getByRole('radio', { name: /Full period/ }))
    expect(
      screen
        .getByRole('table', { name: 'Performance metrics' })
        .querySelector('[data-delta="trades"]')
    ).toHaveTextContent('+50')
  })

  it('common deltas carry no better or worse wording or colour', () => {
    show(make())
    fireEvent.click(screen.getByRole('radio', { name: /Common period/ }))
    const perf = screen.getByRole('table', { name: 'Performance metrics' })
    const cells = perf.querySelectorAll('[data-delta]')
    expect(cells.length).toBeGreaterThan(0)
    for (const c of cells) {
      expect(c.className).not.toMatch(/green|red|emerald|rose/)
      expect(c.textContent ?? '').not.toMatch(/improv|worse|better/i)
    }
  })

  it('shows rules and parameters as separate diffs, and unchanged plainly', () => {
    const { unmount } = show(make())
    const rules = screen.getByRole('table', { name: 'Rules differences' })
    const params = screen.getByRole('table', { name: 'Parameters differences' })
    expect(within(rules).getByText('stop')).toBeInTheDocument()
    expect(within(rules).getByText('filter_x')).toBeInTheDocument()
    expect(within(rules).queryByText('lookback')).not.toBeInTheDocument()
    expect(within(params).getByText('lookback')).toBeInTheDocument()
    expect(within(params).getByText('removed')).toBeInTheDocument()
    unmount()
    const base = make()
    const same = { a: 'x', b: 'x', changed: false }
    show(
      make({
        strategy: {
          ...base.strategy,
          rules: NO_SPEC,
          parameters: NO_SPEC,
          fingerprints: { rules: same, params: same, strategy: same },
        },
      })
    )
    expect(screen.getByText('Rules: unchanged')).toBeInTheDocument()
    expect(screen.getByText('Parameters: unchanged')).toBeInTheDocument()
  })

  it('shows fingerprints in short form with the full value in the title', () => {
    show(make())
    const el = screen.getAllByText('aaaaaaaa')[0]
    expect(el).toHaveAttribute('title', 'aaaaaaaa1111')
  })

  it('notes a changed dataset fingerprint', () => {
    const { unmount } = show(make())
    expect(
      screen.getByText(/history was corrected or backfilled between these runs/)
    ).toBeInTheDocument()
    unmount()
    const base = make()
    show(make({ dataset: { ...base.dataset, fingerprint_changed: false } }))
    expect(screen.queryByText(/corrected or backfilled/)).not.toBeInTheDocument()
  })

  it('emphasises changed scope rows', () => {
    show(make())
    const row = screen.getByText('Universe').closest('tr')
    expect(row).toHaveAttribute('data-changed', 'true')
  })

  it('renders deltas without better or worse wording or colour', () => {
    show(make())
    const perf = screen.getByRole('table', { name: 'Performance metrics' })
    const cells = perf.querySelectorAll('[data-delta]')
    expect(cells.length).toBeGreaterThan(0)
    for (const c of cells) {
      expect(c.className).not.toMatch(/green|red|emerald|rose/)
      expect(c.textContent ?? '').not.toMatch(/improv|worse|better/i)
    }
    expect(perf.textContent).not.toMatch(/improved|worse|better/i)
  })

  it('renders null values as n/a, not 0', () => {
    show(make())
    const perf = screen.getByRole('table', { name: 'Performance metrics' })
    const row = within(perf).getByText('Expectancy (R)').closest('tr') as HTMLElement
    expect(within(row).getAllByText('n/a').length).toBeGreaterThanOrEqual(2)
  })

  it('marks the common period on the date-axis charts', () => {
    show(make())
    const bands = screen.getAllByTestId('span-band')
    expect(bands.length).toBeGreaterThan(0)
    expect(bands[0]).toHaveTextContent('Common period 2024-01-01 to 2024-06-30')
    expect(bands[0]).toHaveTextContent('Incremental period 2024-07-01 to 2024-09-30')
  })

  it('gives charts a text alternative and a legend', () => {
    show(make())
    expect(screen.getAllByText(/Run A/).length).toBeGreaterThan(0)
    expect(screen.getByTestId('summary-equity')).toHaveTextContent(/points/)
    expect(screen.getByRole('table', { name: /Net R distribution/ })).toBeInTheDocument()
  })

  it('shows the changed fingerprint, not unchanged, when the diff dicts are empty', () => {
    const base = make()
    show(make({ strategy: { ...base.strategy, rules: NO_SPEC, parameters: NO_SPEC } }))
    expect(
      screen.getByText(/Fingerprints differ; the detailed change was not recorded/)
    ).toBeInTheDocument()
    expect(screen.getByText('Parameters: unchanged')).toBeInTheDocument()
    expect(screen.queryByText('Rules: unchanged')).not.toBeInTheDocument()
  })

  it('titles the performance table and curves with the selected view', () => {
    show(make())
    expect(screen.getByTestId('performance-title')).toHaveTextContent('Full period')
    fireEvent.click(screen.getByRole('radio', { name: /Common period/ }))
    expect(screen.getByTestId('performance-title')).toHaveTextContent('Common period')
    expect(screen.getByTestId('curves-title')).toHaveTextContent('Common period')
  })

  it('renders each curve set from its own field and as given', () => {
    show(make())
    expect(screen.getByTestId('summary-equity')).toHaveTextContent('3 points')
    fireEvent.click(screen.getByRole('radio', { name: /Common period/ }))
    // the 2024-08-01 point lies outside the common range but is rendered as given
    expect(screen.getByTestId('summary-equity')).toHaveTextContent('4 points')
    expect(screen.getByTestId('summary-equity')).toHaveTextContent('Run A 4 values, last 3.0')
    expect(screen.getByRole('figure', { name: 'Monthly net R' })).toHaveTextContent('2024-03')
    expect(screen.getByRole('figure', { name: 'Monthly net R' })).not.toHaveTextContent('2024-07')
    fireEvent.click(screen.getByRole('radio', { name: /Incremental period/ }))
    expect(screen.getByTestId('summary-equity')).toHaveTextContent('2 points')
    expect(screen.getByRole('figure', { name: 'Monthly net R' })).toHaveTextContent('2024-08')
  })

  it('says curves start at zero for common and incremental, and not for full', () => {
    show(make())
    expect(screen.queryByText(/start at zero at the start of this window/i)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('radio', { name: /Common period/ }))
    expect(screen.getByText(/start at zero at the start of this window/i)).toBeInTheDocument()
    expect(screen.queryByText(/not re-based/i)).not.toBeInTheDocument()
  })

  it('renders every range as its own labelled strip', () => {
    const base = make()
    show(
      make({
        curves_incremental: {
          ...(base.curves_incremental as NonNullable<RunComparison['curves_incremental']>),
          incremental: [
            { start: '2023-01-01', end: '2023-03-31' },
            { start: '2024-07-01', end: '2024-09-30' },
          ],
        },
      })
    )
    fireEvent.click(screen.getByRole('radio', { name: /Incremental period/ }))
    const strips = document.querySelectorAll('[data-strip="incremental"]')
    expect(strips.length).toBe(2 * 3)
    expect(strips[0]).toHaveTextContent('2023-01-01 to 2023-03-31')
    expect(strips[1]).toHaveTextContent('2024-07-01 to 2024-09-30')
  })

  it('draws drawdown as zero or negative with a signed axis note', () => {
    show(make())
    expect(screen.getByTestId('chart-Drawdown (R, zero or negative)')).toBeInTheDocument()
  })

  it('resets the view when the compared pair changes', () => {
    const c = make()
    const { rerender } = render(<CompareDetail comparison={c} runA={RUN_A} runB={RUN_B} />)
    fireEvent.click(screen.getByRole('radio', { name: /Common period/ }))
    expect(screen.getByRole('radio', { name: /Common period/ })).toBeChecked()
    const noCommon = make({
      views: { ...c.views, common: null },
      curves_common: null,
      performance_common: null,
    })
    rerender(
      <CompareDetail comparison={noCommon} runA={RUN_A} runB={{ ...RUN_B, run_id: 'BT-3' }} />
    )
    expect(screen.getByRole('radio', { name: /Full period/ })).toBeChecked()
    expect(screen.getByTestId('performance-title')).toHaveTextContent('Full period')
    expect(
      within(screen.getByRole('table', { name: 'Performance metrics' })).getByText('150')
    ).toBeInTheDocument()
  })

  it('falls back to the full view when the chosen view disappears for the same pair', () => {
    const c = make()
    const { rerender } = render(<CompareDetail comparison={c} runA={RUN_A} runB={RUN_B} />)
    fireEvent.click(screen.getByRole('radio', { name: /Common period/ }))
    rerender(
      <CompareDetail
        comparison={make({ views: { ...c.views, common: null }, curves_common: null })}
        runA={RUN_A}
        runB={RUN_B}
      />
    )
    expect(screen.getByRole('radio', { name: /Full period/ })).toBeChecked()
    expect(screen.getByRole('radio', { name: /Common period/ })).not.toBeChecked()
    expect(screen.getByTestId('performance-title')).toHaveTextContent('Full period')
    expect(screen.getByTestId('curves-title')).toHaveTextContent('Full period')
    expect(screen.getByTestId('summary-equity')).toHaveTextContent('3 points')
  })

  it('uses full curves, labelled as such, when only the curves of a view are missing', () => {
    const c = make()
    show(make({ curves_common: null }))
    fireEvent.click(screen.getByRole('radio', { name: /Common period/ }))
    expect(screen.getByTestId('performance-title')).toHaveTextContent('Common period')
    expect(screen.getByTestId('curves-title')).toHaveTextContent('Full period')
    expect(c.curves_common).not.toBeNull()
  })

  it('shows and copies the full hash on request', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    show(make())
    const show1 = screen.getAllByRole('button', { name: /Show full value/ })[0]
    fireEvent.click(show1)
    expect(screen.getByText('aaaaaaaa1111')).toBeInTheDocument()
    fireEvent.click(screen.getAllByRole('button', { name: 'Copy full value' })[0])
    expect(writeText).toHaveBeenCalledWith('aaaaaaaa1111')
  })
})
