import { describe, expect, it } from 'vitest'
import type { ValidationCheck, ValidationStatus } from '@/api/strategyPnl'
import { render, screen } from '@/test/test-utils'
import { ValidationBadge, ValidationStatusCard } from './ValidationStatusCard'

const check = (
  id: string,
  status: ValidationCheck['status'],
  dimension: ValidationCheck['dimension'] = 'robustness'
): ValidationCheck => ({
  id,
  dimension,
  label: `Label ${id}`,
  status,
  detail: `Detail ${id}`,
  needed_for: 'validated',
})

function validation(extra: Partial<ValidationStatus> = {}): ValidationStatus {
  const checks = [
    check('a_pass', 'pass', 'performance'),
    check('b_fail', 'fail'),
    check('c_wait', 'pending', 'statistical'),
    check('d_na', 'not_assessed', 'execution'),
  ]
  return {
    status: 'under_validation',
    label: 'Under validation',
    summary: 'The average is positive and the history is long enough.',
    next_level: 'validated',
    next_level_label: 'Validated',
    dimensions: {
      performance: 'pass',
      statistical: 'pending',
      robustness: 'fail',
      execution: 'not_assessed',
    },
    checks,
    failed: ['b_fail'],
    waiting: ['c_wait', 'd_na'],
    blocking: ['b_fail', 'c_wait', 'd_na'],
    passed_count: 1,
    total_count: 4,
    mode: 'analyze',
    ...extra,
  }
}

describe('ValidationStatusCard', () => {
  it('shows the status, the summary and the four dimensions', () => {
    render(<ValidationStatusCard validation={validation()} />)
    expect(screen.getByText('Validation status')).toBeInTheDocument()
    expect(screen.getAllByText('Under validation').length).toBeGreaterThan(0)
    expect(screen.getByText(/history is long enough/)).toBeInTheDocument()
    for (const name of ['Performance', 'Statistics', 'Robustness', 'Execution']) {
      expect(screen.getByText(new RegExp(`^${name}:`))).toBeInTheDocument()
    }
    expect(screen.getByText('1 of 4 checks passed')).toBeInTheDocument()
  })

  it('lists what stops the next level, with failed and pending marked differently', () => {
    render(<ValidationStatusCard validation={validation()} />)
    expect(screen.getByText('Needed to reach "Validated"')).toBeInTheDocument()
    // the failed check is part of the next-level list, so there is no separate Failed heading
    expect(screen.queryByText('Failed', { selector: 'div' })).not.toBeInTheDocument()
    expect(screen.getAllByText('Failed').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Pending').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Not assessed').length).toBeGreaterThan(0)
    expect(screen.getByText('Detail b_fail')).toBeInTheDocument()
  })

  it('shows a failed check separately from the next-level list when it does not block it', () => {
    const v = validation({ blocking: ['c_wait'] })
    render(<ValidationStatusCard validation={v} />)
    expect(screen.getByText('Failed', { selector: 'div' })).toBeInTheDocument() // its own heading
    expect(screen.getByText('Detail b_fail')).toBeInTheDocument()
  })

  it('says Sandbox fills cannot establish execution quality, and nothing about it for Live', () => {
    const { rerender } = render(<ValidationStatusCard validation={validation()} />)
    expect(screen.getByText(/Sandbox fills are simulated, so live execution/)).toBeInTheDocument()
    rerender(<ValidationStatusCard validation={validation({ mode: 'live' })} />)
    expect(screen.queryByText(/Sandbox fills are simulated/)).not.toBeInTheDocument()
  })

  it('says live execution cannot be assessed for an unknown mode, separately from Sandbox', () => {
    render(<ValidationStatusCard validation={validation({ mode: null })} />)
    expect(screen.getByText(/trading mode is not known/)).toBeInTheDocument()
    expect(screen.queryByText(/Sandbox fills are simulated/)).not.toBeInTheDocument()
  })

  it('shows a failed check without crashing on a status it does not know', () => {
    const odd = validation({
      checks: [{ ...check('x', 'pass'), status: 'weird' as ValidationCheck['status'] }],
      failed: [],
      waiting: [],
      blocking: [],
    })
    render(<ValidationStatusCard validation={odd} />)
    expect(screen.getAllByText('Not assessed').length).toBeGreaterThan(0)
  })

  it('does not show a Validated badge for an under-validation selection', () => {
    render(<ValidationStatusCard validation={validation()} />)
    expect(screen.queryByText('Validated', { selector: 'span' })).not.toBeInTheDocument()
  })
})

describe('ValidationBadge', () => {
  it('labels a state, shows a dash without one, and carries the summary as a tooltip', () => {
    const { rerender } = render(
      <ValidationBadge state="preliminary" label="Preliminary" title="Early results" />
    )
    expect(screen.getByText('Preliminary')).toHaveAttribute('title', 'Early results')
    rerender(<ValidationBadge state={undefined} />)
    expect(screen.getByText('—')).toBeInTheDocument()
  })
})
