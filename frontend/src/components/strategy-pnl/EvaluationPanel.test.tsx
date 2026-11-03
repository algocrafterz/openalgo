import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const prepareEvaluation = vi.fn()
const validateEvaluation = vi.fn()
vi.mock('@/api/strategyPnl', () => ({
  strategyPnlApi: {
    prepareEvaluation: (...a: unknown[]) => prepareEvaluation(...a),
    validateEvaluation: (...a: unknown[]) => validateEvaluation(...a),
  },
}))

const { EvaluationPanel } = await import('./EvaluationPanel')

const PROMPT = 'PROMPT WITHOUT DATA'
const PROMPT_DATA = 'PROMPT WITH DATA'

function makePackage(overrides: { withData?: string | null } = {}) {
  return {
    meta: {
      generated_at: '2026-11-03T10:00:00+05:30',
      mode_display: 'Live',
      period: '30d',
      start_date: '2026-10-05',
      end_date: '2026-11-03',
      capital: 100000,
      strategies_included: ['Alpha'],
      package_hash: 'abc',
      truncated: false,
    },
    dataset: {},
    report: {
      schema_version: 1,
      scope: {
        n_trades: 12,
        trading_days: 8,
        strategies: 1,
        labels: { pooled: 'insufficient data', per_strategy: { Alpha: 'no distinguishable edge' } },
      },
      sections: [
        {
          id: 'sample_size',
          title: 'Sample size',
          findings: [
            {
              id: 'a',
              kind: 'math',
              scope: 'pooled',
              statement: 'Math statement',
              n: 12,
              evidence: [],
            },
          ],
        },
        {
          id: 'weaknesses',
          title: 'Areas to investigate',
          findings: [
            {
              id: 'b',
              kind: 'heuristic',
              scope: 'pooled',
              statement: 'Heuristic statement',
              n: 12,
              evidence: [],
              rule: { name: 'profit_concentration', parameters: { CONCENTRATION_FLAG: 0.5 } },
            },
            {
              id: 'c',
              kind: 'not_available',
              scope: 'pooled',
              statement: 'Unknown thing',
              n: 0,
              evidence: [],
            },
          ],
        },
      ],
      limitations: ['Fixed rules only.'],
      legend: {
        math: 'A number or fact computed directly from your trades.',
        heuristic: 'A rule-of-thumb check. The cut-off is a choice, not a statistical proof.',
        not_available: 'Something this data cannot tell you. Nothing is guessed.',
      },
    },
    prompt: {
      text: PROMPT,
      text_with_data: overrides.withData === undefined ? PROMPT_DATA : overrides.withData,
    },
    export: {
      data_filename: 'performance-evaluation-live.json',
      data_json: '{"x":1}',
      notice: 'SERVER NOTICE about the file.',
    },
  }
}

const baseProps = {
  filter: { include: ['Alpha'], exclude: [] as string[] },
  period: '30d' as const,
  range: undefined,
  capital: null,
  clientMode: 'live' as const,
  nothingSelected: false,
  tradesCount: 12,
  ready: true,
}

function renderPanel(props: Partial<typeof baseProps> = {}) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  const ui = (p: typeof baseProps) => (
    <QueryClientProvider client={client}>
      <EvaluationPanel {...p} />
    </QueryClientProvider>
  )
  const view = render(ui({ ...baseProps, ...props }))
  return {
    ...view,
    update: (next: Partial<typeof baseProps>) =>
      view.rerender(ui({ ...baseProps, ...props, ...next })),
  }
}

async function prepare(pkg = makePackage()) {
  prepareEvaluation.mockResolvedValue({ status: 'success', package: pkg })
  fireEvent.click(screen.getByRole('button', { name: 'Prepare AI Evaluation' }))
  await screen.findByText('Quick evaluation (calculated here)')
}

const clipboardOriginal = Object.getOwnPropertyDescriptor(navigator, 'clipboard')

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(() => {
  if (clipboardOriginal) Object.defineProperty(navigator, 'clipboard', clipboardOriginal)
  else Reflect.deleteProperty(navigator, 'clipboard')
  Reflect.deleteProperty(document, 'execCommand')
})

describe('disabled reasons', () => {
  it('waits for the page data', () => {
    renderPanel({ ready: false })
    expect(screen.getByText('Waiting for the page data')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Prepare AI Evaluation' })).toBeDisabled()
  })

  it('asks for a strategy when nothing is selected', () => {
    renderPanel({ nothingSelected: true })
    expect(screen.getByText('Select at least one strategy.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Prepare AI Evaluation' })).toBeDisabled()
  })

  it('says there are no trades', () => {
    renderPanel({ tradesCount: 0 })
    expect(
      screen.getByText('There are no closed trades in this selection to evaluate.')
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Prepare AI Evaluation' })).toBeDisabled()
  })
})

describe('prepare and report', () => {
  it('calls the API once per press and shows the report', async () => {
    renderPanel()
    await prepare()
    expect(prepareEvaluation).toHaveBeenCalledTimes(1)
    expect(prepareEvaluation).toHaveBeenCalledWith(baseProps.filter, '30d', undefined, null)
    expect(screen.getByText('Math statement')).toBeInTheDocument()
    expect(
      screen.getAllByText('Calculated', { selector: '[data-slot="badge"]' }).length
    ).toBeGreaterThan(0)
  })

  it('shows rule parameters and the not-proof sentence for a rule of thumb', async () => {
    renderPanel()
    await prepare()
    expect(screen.getByText(/profit_concentration/)).toHaveTextContent('CONCENTRATION_FLAG = 0.5')
    expect(screen.getAllByText(/not a statistical proof/).length).toBeGreaterThan(0)
    expect(
      screen.getAllByText('Rule of thumb', { selector: '[data-slot="badge"]' }).length
    ).toBeGreaterThan(0)
    expect(
      screen.getAllByText('Not available', { selector: '[data-slot="badge"]' }).length
    ).toBeGreaterThan(0)
  })

  it('uses only the four evidence labels and never grades', async () => {
    renderPanel()
    await prepare()
    expect(screen.getByText('insufficient data')).toBeInTheDocument()
    expect(screen.getByText('no distinguishable edge')).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/\b(good|average|poor)\b/i)
  })

  it('shows the server message when preparing fails', async () => {
    prepareEvaluation.mockResolvedValue({ status: 'error', message: 'Server says wait.' })
    renderPanel()
    fireEvent.click(screen.getByRole('button', { name: 'Prepare AI Evaluation' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Server says wait.')
  })

  it('shows a stale banner after the selection changes', async () => {
    const view = renderPanel()
    await prepare()
    expect(screen.queryByText(/The selection changed after this was prepared/)).toBeNull()
    view.update({ capital: 250000 })
    expect(screen.getByText(/The selection changed after this was prepared/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Copy prompt (stale)' })).toBeEnabled()
  })

  it('puts the disclosure before the copy and download buttons', async () => {
    renderPanel()
    await prepare()
    const notice = screen.getByText('SERVER NOTICE about the file.')
    const disclosure = screen.getByText(/goes to Anthropic under your own account/)
    const copy = screen.getByRole('button', { name: 'Copy prompt' })
    const download = screen.getByRole('button', { name: 'Download evaluation data' })
    const before = Node.DOCUMENT_POSITION_FOLLOWING
    expect(notice.compareDocumentPosition(copy) & before).toBeTruthy()
    expect(disclosure.compareDocumentPosition(copy) & before).toBeTruthy()
    expect(disclosure.compareDocumentPosition(download) & before).toBeTruthy()
  })
})

describe('copy prompt', () => {
  it('copies the prompt with data by default', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    renderPanel()
    await prepare()
    fireEvent.click(screen.getByRole('button', { name: 'Copy prompt' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(PROMPT_DATA))
  })

  it('copies the prompt without data when the box is off, and says to attach the file', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    renderPanel()
    await prepare()
    fireEvent.click(screen.getByLabelText('Include the data in the prompt'))
    fireEvent.click(screen.getByRole('button', { name: 'Copy prompt' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(PROMPT))
    expect(screen.getAllByText(/Attach the downloaded file/).length).toBeGreaterThan(0)
  })

  it('falls back to the prompt without data when no data version exists', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    renderPanel()
    await prepare(makePackage({ withData: null }))
    fireEvent.click(screen.getByRole('button', { name: 'Copy prompt' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(PROMPT))
    expect(screen.getAllByText(/too large to paste/).length).toBeGreaterThan(0)
  })

  it('shows a selected read-only box and a Ctrl+C note when copying is unavailable', async () => {
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    document.execCommand = vi.fn().mockReturnValue(false)
    renderPanel()
    await prepare()
    fireEvent.click(screen.getByRole('button', { name: 'Copy prompt' }))
    const box = await screen.findByLabelText('Prompt to copy')
    expect(box).toHaveValue(PROMPT_DATA)
    expect(box).toHaveAttribute('readonly')
    expect(screen.getByText(/Press Ctrl\+C/)).toBeInTheDocument()
  })
})

describe('download', () => {
  it('builds a Blob with the server file name and revokes the URL', async () => {
    const create = vi.fn().mockReturnValue('blob:x')
    const revoke = vi.fn()
    const originalCreate = URL.createObjectURL
    const originalRevoke = URL.revokeObjectURL
    URL.createObjectURL = create
    URL.revokeObjectURL = revoke
    const names: string[] = []
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement
    ) {
      names.push(this.download)
    })
    try {
      renderPanel()
      await prepare()
      fireEvent.click(screen.getByRole('button', { name: 'Download evaluation data' }))
      expect(create).toHaveBeenCalledTimes(1)
      const blob = create.mock.calls[0][0] as Blob
      expect(blob.size).toBe('{"x":1}'.length)
      expect(names).toEqual(['performance-evaluation-live.json'])
      expect(revoke).toHaveBeenCalledWith('blob:x')
    } finally {
      click.mockRestore()
      URL.createObjectURL = originalCreate
      URL.revokeObjectURL = originalRevoke
    }
  })
})

const CHECKS_OK = { package_match: true, label_conflicts: [], warnings: [] }

function evaluationWith(text: string) {
  return {
    schema_version: 1,
    package_hash: 'abc',
    summary: 'Summary <script>alert(1)</script>',
    overall_confidence: 'low',
    strategies: [
      {
        name: 'Alpha',
        assessment: 'possible edge (screening only)',
        n: 12,
        confidence: 'low',
        observations: [{ text, kind: 'hypothesis', evidence: ['strategies[0].x'], n: 5 }],
        weaknesses: ['weak <b>one</b>'],
        suggested_tests: ['run a backtest'],
        deterministic_label: 'no distinguishable edge',
        conflict: true,
      },
    ],
    portfolio: null,
    limitations: ['limited'],
  }
}

async function paste(text: string) {
  fireEvent.change(screen.getByLabelText("Claude's analysis"), { target: { value: text } })
  fireEvent.click(screen.getByRole('button', { name: 'Check and show' }))
}

describe('pasted analysis', () => {
  it('renders markup as literal text and creates no elements', async () => {
    const hostile = '<img src=x onerror=alert(1)> and <script>alert(2)</script>'
    validateEvaluation.mockResolvedValue({
      status: 'success',
      evaluation: evaluationWith(hostile),
      checks: CHECKS_OK,
    })
    const view = renderPanel()
    await prepare()
    await paste('{"x":1}')
    await screen.findByText(/not calculated by OpenAlgo/)
    expect(screen.getByText(hostile)).toBeInTheDocument()
    expect(view.container.querySelector('img,script')).toBeNull()
    expect(view.container.querySelector('b')).toBeNull()
    expect(view.container.querySelector('a[href]')).toBeNull()
  })

  it('shows the conflict badge, the calculated label and the observation kind', async () => {
    validateEvaluation.mockResolvedValue({
      status: 'success',
      evaluation: evaluationWith('plain'),
      checks: CHECKS_OK,
    })
    renderPanel()
    await prepare()
    await paste('{"x":1}')
    await screen.findByText('Differs from the calculated label')
    expect(screen.getByText('Hypothesis to test')).toBeInTheDocument()
    expect(screen.getByText('Ideas to test, not instructions')).toBeInTheDocument()
    const section = screen.getByRole('region', { name: "Claude's analysis" })
    expect(within(section).getByText('possible edge (screening only)')).toBeInTheDocument()
  })

  it('warns when the analysis was written for different data', async () => {
    validateEvaluation.mockResolvedValue({
      status: 'success',
      evaluation: evaluationWith('plain'),
      checks: {
        package_match: false,
        label_conflicts: [],
        warnings: ['This analysis was written for different data than the current selection.'],
      },
    })
    renderPanel()
    await prepare()
    await paste('{"x":1}')
    const note = await screen.findByText(
      'This analysis was written for different data than the current selection.'
    )
    expect(note).toBeInTheDocument()
    expect(
      screen.getAllByText(
        'This analysis was written for different data than the current selection.'
      )
    ).toHaveLength(1)
  })

  it('shows problems for an invalid analysis and keeps what was pasted', async () => {
    validateEvaluation.mockResolvedValue({
      status: 'error',
      message: 'The pasted analysis could not be accepted.',
      problems: [{ path: 'summary', message: 'This field is missing.' }],
    })
    renderPanel()
    await prepare()
    await paste('my pasted text')
    const alert = await screen.findByText('The pasted analysis could not be accepted.')
    expect(alert).toBeInTheDocument()
    expect(screen.getByText('summary: This field is missing.')).toBeInTheDocument()
    expect(screen.getByLabelText("Claude's analysis")).toHaveValue('my pasted text')
    expect(screen.queryByText(/not calculated by OpenAlgo/)).toBeNull()
  })

  it('drops the pasted analysis result when a new package is prepared', async () => {
    validateEvaluation.mockResolvedValue({
      status: 'success',
      evaluation: evaluationWith('plain'),
      checks: CHECKS_OK,
    })
    renderPanel()
    await prepare()
    await paste('{"x":1}')
    await screen.findByText(/not calculated by OpenAlgo/)
    fireEvent.click(screen.getByRole('button', { name: 'Prepare again' }))
    await waitFor(() => expect(screen.queryByText(/not calculated by OpenAlgo/)).toBeNull())
  })
})

describe('wording', () => {
  it('has no emoji, icons or banned words in the rendered text', async () => {
    validateEvaluation.mockResolvedValue({
      status: 'success',
      evaluation: evaluationWith('plain'),
      checks: CHECKS_OK,
    })
    const view = renderPanel()
    await prepare()
    await paste('{"x":1}')
    await screen.findByText(/not calculated by OpenAlgo/)
    const text = view.container.textContent ?? ''
    expect(text).not.toMatch(/[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}]/u)
    expect(text).not.toMatch(/\b(arm|armed|arming|paper|analyze)\b/i)
  })
})

describe('copy note and pasted text lifecycle', () => {
  function mockClipboard() {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    return writeText
  }

  it('clears the copy note when the include-data box changes', async () => {
    mockClipboard()
    renderPanel()
    await prepare()
    fireEvent.click(screen.getByRole('button', { name: 'Copy prompt' }))
    await screen.findByText('Prompt copied, with the data inside it.')
    fireEvent.click(screen.getByLabelText('Include the data in the prompt'))
    expect(screen.queryByText('Prompt copied, with the data inside it.')).toBeNull()
  })

  it('clears the copy note when a new package is prepared in the same second', async () => {
    mockClipboard()
    renderPanel()
    await prepare()
    fireEvent.click(screen.getByRole('button', { name: 'Copy prompt' }))
    await screen.findByText('Prompt copied, with the data inside it.')
    fireEvent.click(screen.getByRole('button', { name: 'Prepare again' }))
    await waitFor(() => expect(prepareEvaluation).toHaveBeenCalledTimes(2))
    await waitFor(() =>
      expect(screen.queryByText('Prompt copied, with the data inside it.')).toBeNull()
    )
  })

  it('disables the check and says why when the paste is over 60,000 characters', async () => {
    renderPanel()
    await prepare()
    fireEvent.change(screen.getByLabelText("Claude's analysis"), {
      target: { value: 'a'.repeat(60001) },
    })
    expect(screen.getByRole('button', { name: 'Check and show' })).toBeDisabled()
    expect(screen.getByRole('alert')).toHaveTextContent(
      "That is longer than 60,000 characters. Paste only Claude's answer."
    )
  })

  it('shows the stale banner after a mode switch and keeps the pasted text and analysis', async () => {
    validateEvaluation.mockResolvedValue({
      status: 'success',
      evaluation: evaluationWith('plain'),
      checks: CHECKS_OK,
    })
    const view = renderPanel()
    await prepare()
    await paste('my pasted text')
    await screen.findByText(/not calculated by OpenAlgo/)
    view.update({ clientMode: 'sandbox' as never })
    expect(screen.getByText(/The selection changed after this was prepared/)).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: "Claude's analysis" })).toHaveValue('my pasted text')
    expect(screen.getByText(/not calculated by OpenAlgo/)).toBeInTheDocument()
    expect(screen.getByText('Checked against an earlier selection.')).toBeInTheDocument()
  })

  it('renders markup in the report statement, note and problems as literal text', async () => {
    const hostile =
      '<img src=x onerror=alert(1)><script>alert(2)</script><b>bold</b><a href="x">l</a>'
    const pkg = makePackage()
    pkg.report.sections[0].findings[0].statement = hostile
    Object.assign(pkg.report.sections[0].findings[0], { note: hostile })
    validateEvaluation.mockResolvedValue({
      status: 'error',
      message: 'The pasted analysis could not be accepted.',
      problems: [{ path: 'summary', message: hostile }],
    })
    const view = renderPanel()
    await prepare(pkg)
    await paste('x')
    await screen.findByText('The pasted analysis could not be accepted.')
    expect(screen.getAllByText(hostile).length).toBeGreaterThanOrEqual(2)
    expect(view.container.querySelector('img,script,b,a[href]')).toBeNull()
  })
})
