import { render, screen } from '@testing-library/react'
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'
import { GLOSSARY } from '@/lib/backtestGlossary'
import { InfoTip } from './InfoTip'

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

describe('InfoTip', () => {
  beforeAll(() => {
    vi.stubGlobal('ResizeObserver', ResizeObserverStub)
  })
  afterAll(() => {
    vi.unstubAllGlobals()
  })

  it('renders an accessible trigger labelled with the term', () => {
    render(<InfoTip term="profit_factor" />)
    expect(screen.getByRole('button', { name: 'What is Profit Factor?' })).toBeInTheDocument()
  })

  it('shows the short definition on focus', async () => {
    render(<InfoTip term="edge" />)
    screen.getByRole('button', { name: 'What is EDGE?' }).focus()
    const tip = await screen.findByRole('tooltip')
    expect(tip).toHaveTextContent(GLOSSARY.edge.short)
  })

  it('renders nothing for an unknown id without throwing', () => {
    const { container } = render(<InfoTip term="not-a-term" />)
    expect(container).toBeEmptyDOMElement()
  })
})
