/**
 * The backtest panel's setup: eight essentials in view, everything else under Advanced.
 *
 * The compiled program is real in shape (inputs and a `strategy()` declaration, written the way
 * the compiler writes them) and the run is replaced, because what is under test is what the
 * panel shows before a run and what it sends when Run is pressed, not the engine.
 */

import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { backtestLookbackDays } from '@/lib/trading/intervals'
import { cleanup, render, screen, waitFor, within } from '@/test/test-utils'

const runBacktest = vi.fn()
vi.mock('@/lib/trading/backtestRun', () => ({
  MAX_BARS: 100000,
  runBacktest: (...args: unknown[]) => runBacktest(...args),
}))

const held = vi.hoisted(() => ({ program: null as unknown }))

vi.mock('@/lib/trading/openscriptFiles', () => ({
  listScripts: async () => [
    { file: 'probe.oscript', mtime: 1, bytes: 1 },
    { file: 'other.oscript', mtime: 1, bytes: 1 },
  ],
  readScript: async () => 'version 1\nstrategy("probe")\n',
  kindOf: async () => 'strategy',
  compileSource: async () =>
    held.program === null
      ? { ok: false, diagnostics: [] }
      : { ok: true, program: JSON.stringify(held.program) },
}))

vi.mock('@/hooks/useLivePrice', () => ({
  useLivePrice: () => ({ data: [], isLive: false }),
}))

vi.mock('./BacktestChart', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./BacktestChart')>()),
  BacktestChart: () => null,
}))

const { BacktestPanel } = await import('./BacktestPanel')

const number = (value: number) => ['n', value]

function program(over: { strategy?: Record<string, unknown>; inputs?: unknown[] } = {}) {
  return {
    inputs: over.inputs ?? [
      {
        key: 'length',
        kind: 'number',
        label: 'Fast length',
        default: number(9),
        min: 1,
        max: 200,
        step: 1,
        options: null,
        group: '',
        tooltip: null,
      },
    ],
    meta: {
      strategy: {
        capital: 100000,
        currency: 'INR',
        qty: 10,
        qtyType: 'units',
        product: 'MIS',
        fillOn: 'next_open',
        slippage: 2,
        commission: 0.03,
        commissionType: 'percent',
        pyramiding: 1,
        closeOnSessionEnd: true,
        ...over.strategy,
      },
    },
  }
}

function isoDaysAgo(days: number): string {
  const at = new Date()
  at.setDate(at.getDate() - days)
  return at.toISOString().slice(0, 10)
}

const DEFAULT_CHART = { symbol: 'SBIN', exchange: 'NSE', interval: '5m' }

function openPanel(chart: { symbol: string; exchange: string; interval: string } = DEFAULT_CHART) {
  return render(<BacktestPanel apiKey="key" getChartContext={() => chart} />)
}

async function ready() {
  await screen.findByTestId('backtest-essentials')
  await waitFor(() =>
    expect(screen.getByTestId('setup-capital')).not.toHaveTextContent('Not read yet')
  )
}

beforeEach(() => {
  cleanup()
  runBacktest.mockReset()
  runBacktest.mockResolvedValue({ ok: true })
  held.program = program()
})

describe('the essentials', () => {
  it('show the eight things a run is of, without opening anything', async () => {
    openPanel()
    await ready()
    const box = screen.getByTestId('backtest-essentials')
    expect(within(box).getByLabelText('Strategy')).toHaveValue('probe.oscript')
    expect(within(box).getByTestId('setup-instrument')).toHaveTextContent('SBIN (NSE)')
    expect(within(box).getByTestId('setup-timeframe')).toHaveTextContent('5m')
    expect(within(box).getByLabelText('From')).toBeInTheDocument()
    expect(within(box).getByLabelText('To')).toBeInTheDocument()
    expect(within(box).getByTestId('setup-capital')).toHaveTextContent('100,000 INR')
    expect(within(box).getByTestId('setup-size')).toHaveTextContent('10 units')
    expect(within(box).getByTestId('setup-commission')).toHaveTextContent('0.03 percent')
    expect(box.textContent).not.toMatch(/perTrade|next_open/)
    expect(within(box).getByTestId('setup-slippage')).toHaveTextContent('2 ticks')
    expect(screen.getByRole('button', { name: 'Run backtest' })).toBeEnabled()
  })

  it('say whose each value is, so nothing reads as a box that does nothing', async () => {
    openPanel()
    await ready()
    const source = (key: string) => screen.getByTestId(`setup-${key}-source`).textContent
    expect(source('instrument')).toBe('from the chart')
    expect(source('timeframe')).toBe('from the chart')
    for (const key of ['capital', 'size', 'commission', 'slippage']) {
      expect(source(key)).toBe('set in the script')
    }
    // The values the script owns are shown, not offered: there is no field for any of them.
    const box = screen.getByTestId('backtest-essentials')
    for (const key of ['capital', 'size', 'commission', 'slippage']) {
      expect(within(within(box).getByTestId(`setup-${key}`)).queryByRole('textbox')).toBeNull()
      expect(within(within(box).getByTestId(`setup-${key}`)).queryByRole('spinbutton')).toBeNull()
    }
  })

  it("puts the script's own inputs with the essentials", async () => {
    openPanel()
    await ready()
    const box = screen.getByTestId('backtest-essentials')
    expect(await within(box).findByText('Fast length')).toBeInTheDocument()
  })

  it('shows the instrument and timeframe as absent when there is no chart', async () => {
    render(<BacktestPanel apiKey="key" getChartContext={() => null} />)
    await screen.findByTestId('backtest-essentials')
    expect(screen.getByTestId('setup-instrument')).toHaveTextContent('No chart open')
    expect(screen.getByTestId('setup-timeframe')).toHaveTextContent('No chart open')
    expect(screen.getByRole('button', { name: 'Run backtest' })).toBeDisabled()
  })

  it('says it has not read the script yet, rather than showing zeros, when it will not compile', async () => {
    held.program = null
    openPanel()
    await screen.findByTestId('backtest-essentials')
    for (const key of ['capital', 'size', 'commission', 'slippage']) {
      expect(screen.getByTestId(`setup-${key}`)).toHaveTextContent('Not read yet')
    }
    expect(screen.queryByTestId('backtest-advanced')).toBeNull()
    expect(screen.queryByTestId('setup-costs-note')).toBeNull()
  })
})

describe('the costs this run rests on', () => {
  it('say what is and is not charged when the script declares costs', async () => {
    openPanel()
    await ready()
    const note = screen.getByTestId('setup-costs-note')
    expect(note).toHaveTextContent('adds no charges of its own')
    expect(note).toHaveTextContent('STT')
    expect(note).not.toHaveTextContent('before trading costs')
  })

  it('warn that the figures are before costs when the script declares none', async () => {
    held.program = program({ strategy: { commission: 0, slippage: 0, commissionType: 'percent' } })
    openPanel()
    await ready()
    expect(screen.getByTestId('setup-costs-note')).toHaveTextContent(
      'no commission and no slippage, so these figures are before trading costs'
    )
  })
})

describe('the date range', () => {
  it('shows the default it follows and where it came from', async () => {
    openPanel()
    await ready()
    expect(screen.getByLabelText('From')).toHaveValue(isoDaysAgo(backtestLookbackDays('5m')))
    expect(screen.getByTestId('setup-range-note')).toHaveTextContent(
      'Default for 5m: the last 2 months'
    )
    expect(screen.queryByRole('button', { name: 'Use the default range' })).toBeNull()
  })

  it('keeps the default for a daily frame at two years, as the panel always has', async () => {
    openPanel({ symbol: 'SBIN', exchange: 'NSE', interval: 'D' })
    await ready()
    expect(screen.getByLabelText('From')).toHaveValue(isoDaysAgo(backtestLookbackDays('D')))
    expect(screen.getByTestId('setup-range-note')).toHaveTextContent('the last 2 years')
  })

  it('stays as the trader set it, and goes back to the default on request', async () => {
    const user = userEvent.setup()
    openPanel()
    await ready()
    const from = screen.getByLabelText('From')
    await user.clear(from)
    await user.type(from, '2025-01-02')
    expect(screen.getByTestId('setup-range-note')).toHaveTextContent('Dates set by you')
    await user.click(screen.getByRole('button', { name: 'Use the default range' }))
    await waitFor(() =>
      expect(screen.getByLabelText('From')).toHaveValue(isoDaysAgo(backtestLookbackDays('5m')))
    )
    expect(screen.getByTestId('setup-range-note')).toHaveTextContent('Default for 5m')
  })
})

describe('Advanced settings', () => {
  it('are closed by default and hold what the script declares about how it trades', async () => {
    const user = userEvent.setup()
    openPanel()
    await ready()
    const advanced = screen.getByTestId('backtest-advanced')
    const toggle = within(advanced).getByRole('button', { name: /Advanced settings/ })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(within(advanced).queryByText('Fills on')).toBeNull()
    expect(within(advanced).queryByText('Pyramiding')).toBeNull()

    await user.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(within(advanced).getByText('Fills on')).toBeInTheDocument()
    expect(within(advanced).getByText('next open')).toBeInTheDocument()
    expect(within(advanced).getByText('Pyramiding')).toBeInTheDocument()
    expect(within(advanced).getByText('Product')).toBeInTheDocument()
    expect(within(advanced).getByText('MIS')).toBeInTheDocument()
    expect(within(advanced).getByText('Closes at session end')).toBeInTheDocument()
    expect(within(advanced).getByTestId('setup-size-note')).toBeInTheDocument()
  })

  it('stay closed when the script has inputs: the inputs are essentials, not advanced', async () => {
    openPanel()
    await ready()
    expect(screen.getByRole('button', { name: /Advanced settings/ })).toHaveAttribute(
      'aria-expanded',
      'false'
    )
    expect(await screen.findByText('Fast length')).toBeVisible()
  })
})

describe('what a run is sent', () => {
  it('is the script, the chart, the default range and no inputs until one is changed', async () => {
    openPanel()
    await ready()
    await waitFor(() => expect(runBacktest).toHaveBeenCalled())
    const first = runBacktest.mock.calls[0][0]
    expect(first).toMatchObject({
      file: 'probe.oscript',
      symbol: 'SBIN',
      exchange: 'NSE',
      interval: '5m',
      inputs: {},
    })
    expect(first.startDate).toBe(isoDaysAgo(backtestLookbackDays('5m')))
    expect(first.endDate).toBe(new Date().toISOString().slice(0, 10))
  })

  it('carries only the input the trader changed, and the dates they set', async () => {
    const user = userEvent.setup()
    openPanel()
    await ready()
    await waitFor(() => expect(runBacktest).toHaveBeenCalled())
    runBacktest.mockClear()

    const row = (await screen.findByText('Fast length')).closest('label') as HTMLElement
    await user.type(within(row).getByRole('spinbutton'), '20')
    const from = screen.getByLabelText('From')
    await user.clear(from)
    await user.type(from, '2025-01-02')
    // Editing does not run: a half-typed range or number is not a question yet.
    expect(runBacktest).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: 'Run backtest' }))
    await waitFor(() => expect(runBacktest).toHaveBeenCalledTimes(1))
    const sent = runBacktest.mock.calls[0][0]
    expect(sent.inputs).toEqual({ length: 20 })
    expect(sent.startDate).toBe('2025-01-02')
  })

  it('does not send capital, size, commission or slippage: the script owns them', async () => {
    const user = userEvent.setup()
    openPanel()
    await ready()
    await user.click(screen.getByRole('button', { name: 'Run backtest' }))
    await waitFor(() => expect(runBacktest).toHaveBeenCalled())
    const sent = runBacktest.mock.calls.at(-1)?.[0]
    for (const key of ['capital', 'qty', 'commission', 'slippage', 'size']) {
      expect(Object.keys(sent.inputs)).not.toContain(key)
      expect(Object.keys(sent)).not.toContain(key)
    }
  })
})

describe('a size the script takes from one of its inputs', () => {
  beforeEach(() => {
    held.program = program({
      strategy: { qty: { input: 'size' } },
      inputs: [
        {
          key: 'size',
          kind: 'number',
          label: 'Order size',
          default: number(5),
          min: 1,
          max: null,
          step: 1,
          options: null,
          group: '',
          tooltip: null,
        },
      ],
    })
  })

  it('is shown as set by the trader, at the default until they type one', async () => {
    const user = userEvent.setup()
    openPanel()
    await waitFor(() => expect(screen.getByTestId('setup-size')).toHaveTextContent('5 units'))
    expect(screen.getByTestId('setup-size-source')).toHaveTextContent('set by you')
    expect(screen.getByTestId('setup-size')).toHaveTextContent('Taken from one of the script')

    const row = (await screen.findByText('Order size', { selector: 'span' })).closest(
      'label'
    ) as HTMLElement
    await user.type(within(row).getByRole('spinbutton'), '12')
    await waitFor(() => expect(screen.getByTestId('setup-size')).toHaveTextContent('12 units'))
  })
})
