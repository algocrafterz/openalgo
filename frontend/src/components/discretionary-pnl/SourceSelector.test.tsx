import { describe, expect, it, vi } from 'vitest'
import { render, screen, userEvent } from '@/test/test-utils'
import { SourceSelector } from './SourceSelector'

describe('SourceSelector', () => {
  it('renders nothing when the feature is disabled', () => {
    const { container } = render(
      <SourceSelector enabled={false} value="strategies" onChange={vi.fn()} />
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('offers the two sources and marks the current one', () => {
    render(<SourceSelector enabled value="strategies" onChange={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Strategies (current mode)' })).toHaveAttribute(
      'aria-pressed',
      'true'
    )
    expect(screen.getByRole('button', { name: 'Discretionary' })).toHaveAttribute(
      'aria-pressed',
      'false'
    )
  })

  it('reports a change, but not a click on the current source', async () => {
    const onChange = vi.fn()
    render(<SourceSelector enabled value="strategies" onChange={onChange} />)
    await userEvent.click(screen.getByRole('button', { name: 'Strategies (current mode)' }))
    expect(onChange).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Discretionary' }))
    expect(onChange).toHaveBeenCalledWith('discretionary')
  })
})
