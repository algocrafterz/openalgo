import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ShortHash } from './ShortHash'

const FULL = '0123456789abcdef0123'

describe('ShortHash', () => {
  afterEach(() => {
    Reflect.deleteProperty(navigator, 'clipboard')
  })

  it('shows the short form with the full value as title', () => {
    render(<ShortHash value={FULL} />)
    expect(screen.getByText('01234567')).toHaveAttribute('title', FULL)
  })

  it('shows not captured for a missing value, without a button', () => {
    render(<ShortHash value={null} />)
    expect(screen.getByText('not captured')).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('toggles the full value with a button', () => {
    render(<ShortHash value={FULL} />)
    fireEvent.click(screen.getByRole('button', { name: 'Show full value' }))
    expect(screen.getByText(FULL)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Hide full value' }))
    expect(screen.getByText('01234567')).toBeInTheDocument()
  })

  it('copies the full value when the clipboard is available', () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    render(<ShortHash value={FULL} />)
    fireEvent.click(screen.getByRole('button', { name: 'Copy full value' }))
    expect(writeText).toHaveBeenCalledWith(FULL)
  })

  it('offers no copy button without a clipboard', () => {
    render(<ShortHash value={FULL} />)
    expect(screen.queryByRole('button', { name: 'Copy full value' })).not.toBeInTheDocument()
  })
})
