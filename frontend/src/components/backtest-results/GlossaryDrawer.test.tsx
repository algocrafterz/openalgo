import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { GlossaryDrawer } from './GlossaryDrawer'

describe('GlossaryDrawer', () => {
  it('is closed by default', () => {
    render(<GlossaryDrawer />)
    expect(screen.getByRole('button', { name: 'Glossary' })).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByText('Profit Factor')).not.toBeInTheDocument()
  })

  it('opens on click and lists terms grouped by category', async () => {
    const user = userEvent.setup()
    render(<GlossaryDrawer />)
    await user.click(screen.getByRole('button', { name: 'Glossary' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Profit Factor')).toBeInTheDocument()
    expect(within(dialog).getAllByRole('heading', { level: 3 }).length).toBeGreaterThan(1)
  })

  it('filters by search text', async () => {
    const user = userEvent.setup()
    render(<GlossaryDrawer />)
    await user.click(screen.getByRole('button', { name: 'Glossary' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByRole('searchbox'), 'slippage')
    expect(within(dialog).getByText('Slippage')).toBeInTheDocument()
    expect(within(dialog).queryByText('Profit Factor')).not.toBeInTheDocument()
  })

  it('shows an empty message when nothing matches', async () => {
    const user = userEvent.setup()
    render(<GlossaryDrawer />)
    await user.click(screen.getByRole('button', { name: 'Glossary' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByRole('searchbox'), 'zzzzqq')
    expect(within(dialog).getByText('No matching terms.')).toBeInTheDocument()
  })

  it('closes on Escape', async () => {
    const user = userEvent.setup()
    render(<GlossaryDrawer />)
    await user.click(screen.getByRole('button', { name: 'Glossary' }))
    await screen.findByRole('dialog')
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
})
