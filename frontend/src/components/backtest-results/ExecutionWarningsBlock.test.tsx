import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ExecutionWarningsBlock } from './ExecutionWarningsBlock'
import { CLEAN_WARNINGS, WARNINGS } from './fixtures/engineBlocks'

describe('ExecutionWarningsBlock', () => {
  it('shows counts and shares and the upper bound beside the actual figure', () => {
    render(<ExecutionWarningsBlock warnings={WARNINGS} />)
    expect(screen.getByTestId('ew-ambiguous')).toHaveTextContent('12 of 200 (6.0%)')
    expect(screen.getByTestId('ew-gap')).toHaveTextContent('4 of 200 (2.0%)')
    expect(screen.getByTestId('ew-skipped')).toHaveTextContent('3')
    const s = screen.getByTestId('ew-sensitivity')
    expect(s).toHaveTextContent('-0.45 R')
    expect(s).toHaveTextContent('Upper bound if all had exited at the target')
    expect(s).toHaveTextContent('0.80 R')
    expect(s).toHaveTextContent('not a result')
    expect(screen.getByTestId('ew-exact')).toHaveTextContent(/^Exact:/)
  })

  it('states when the bound is only approximate', () => {
    const w = {
      ...WARNINGS,
      ambiguous_sensitivity: { ...WARNINGS.ambiguous_sensitivity, exact: false },
    }
    render(<ExecutionWarningsBlock warnings={w as typeof WARNINGS} />)
    expect(screen.getByTestId('ew-exact')).toHaveTextContent(/^Approximate:/)
  })

  it('shows no bound when no trade could be measured', () => {
    render(<ExecutionWarningsBlock warnings={CLEAN_WARNINGS} />)
    expect(screen.getByTestId('ew-sensitivity')).toHaveTextContent('no upper bound to show')
  })

  it('reads not recorded for an unrecorded skipped-entry count', () => {
    render(<ExecutionWarningsBlock warnings={{ ...WARNINGS, entries_skipped_missing_bar: null }} />)
    expect(screen.getByTestId('ew-skipped')).toHaveTextContent('not recorded for this run')
  })

  it('reads not recorded for this run without the block, inventing no figures', () => {
    render(<ExecutionWarningsBlock warnings={null} />)
    const box = screen.getByTestId('execution-warnings')
    expect(box).toHaveTextContent('not recorded for this run')
    expect(screen.queryByTestId('ew-sensitivity')).not.toBeInTheDocument()
    expect(box).not.toHaveTextContent('%')
  })
})
