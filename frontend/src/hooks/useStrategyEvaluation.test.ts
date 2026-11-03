import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook, waitFor } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const prepareEvaluation = vi.fn()
const validateEvaluation = vi.fn()
vi.mock('@/api/strategyPnl', () => ({
  strategyPnlApi: {
    prepareEvaluation: (...a: unknown[]) => prepareEvaluation(...a),
    validateEvaluation: (...a: unknown[]) => validateEvaluation(...a),
  },
}))

const { useStrategyEvaluation } = await import('./useStrategyEvaluation')

const PKG = { meta: { generated_at: 't1' }, report: {}, prompt: {}, export: {} }
const base = {
  filter: { include: ['A'], exclude: [] as string[] },
  period: '30d' as const,
  range: undefined,
  capital: null,
  mode: 'live' as const,
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  return createElement(QueryClientProvider, { client }, children)
}

beforeEach(() => {
  vi.clearAllMocks()
  prepareEvaluation.mockResolvedValue({ status: 'success', package: PKG })
})

describe('useStrategyEvaluation', () => {
  it('stores the package after prepare and is not stale', async () => {
    const { result } = renderHook(() => useStrategyEvaluation(base), { wrapper })
    await act(() => result.current.prepare())
    expect(result.current.pkg).toBe(PKG)
    expect(result.current.stale).toBe(false)
    expect(prepareEvaluation).toHaveBeenCalledWith(base.filter, '30d', undefined, null)
  })

  it('turns stale when the selection changes', async () => {
    const { result, rerender } = renderHook((p) => useStrategyEvaluation(p), {
      wrapper,
      initialProps: base,
    })
    await act(() => result.current.prepare())
    rerender({ ...base, capital: 5000 as never })
    expect(result.current.stale).toBe(true)
    rerender(base)
    expect(result.current.stale).toBe(false)
  })

  it('shows the server message when prepare fails', async () => {
    prepareEvaluation.mockResolvedValue({ status: 'error', message: 'Too many requests, wait.' })
    const { result } = renderHook(() => useStrategyEvaluation(base), { wrapper })
    await act(() => result.current.prepare())
    expect(result.current.pkg).toBeNull()
    expect(result.current.prepareError).toBe('Too many requests, wait.')
  })

  it('keeps a validated analysis and clears it when a new package is prepared', async () => {
    validateEvaluation.mockResolvedValue({
      status: 'success',
      evaluation: { summary: 's' },
      checks: { package_match: true, label_conflicts: [], warnings: [] },
    })
    const { result } = renderHook(() => useStrategyEvaluation(base), { wrapper })
    await act(() => result.current.prepare())
    await act(() => result.current.validate('pasted'))
    expect(result.current.imported?.evaluation).toEqual({ summary: 's' })
    expect(validateEvaluation).toHaveBeenCalledWith(base.filter, '30d', undefined, null, 'pasted')
    await act(() => result.current.prepare())
    expect(result.current.imported).toBeNull()
  })

  it('records the problems of a rejected analysis', async () => {
    validateEvaluation.mockResolvedValue({
      status: 'error',
      message: 'Fix the items.',
      problems: [{ path: 'summary', message: 'missing' }],
    })
    const { result } = renderHook(() => useStrategyEvaluation(base), { wrapper })
    await act(() => result.current.validate('x'))
    expect(result.current.imported).toBeNull()
    expect(result.current.failure).toEqual({
      message: 'Fix the items.',
      problems: [{ path: 'summary', message: 'missing' }],
    })
  })

  it('ignores a second prepare while one is pending', async () => {
    let release: (v: unknown) => void = () => undefined
    prepareEvaluation.mockReturnValue(
      new Promise((resolve) => {
        release = resolve
      })
    )
    const { result } = renderHook(() => useStrategyEvaluation(base), { wrapper })
    let first: Promise<void> = Promise.resolve()
    act(() => {
      first = result.current.prepare()
      void result.current.prepare()
    })
    await waitFor(() => expect(prepareEvaluation).toHaveBeenCalledTimes(1))
    expect(result.current.preparing).toBe(true)
    await act(async () => {
      release({ status: 'success', package: PKG })
      await first
    })
    await waitFor(() => expect(result.current.pkg).toBe(PKG))
  })

  it('persists nothing in the browser', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    const { result } = renderHook(() => useStrategyEvaluation(base), { wrapper })
    await act(() => result.current.prepare())
    expect(setItem).not.toHaveBeenCalled()
    setItem.mockRestore()
  })

  it('drops a check that returns after a newer package was prepared', async () => {
    let release: (v: unknown) => void = () => undefined
    validateEvaluation.mockReturnValue(
      new Promise((resolve) => {
        release = resolve
      })
    )
    const { result } = renderHook(() => useStrategyEvaluation(base), { wrapper })
    await act(() => result.current.prepare())
    let pending: Promise<void> = Promise.resolve()
    act(() => {
      pending = result.current.validate('pasted')
    })
    await waitFor(() => expect(validateEvaluation).toHaveBeenCalledTimes(1))
    await act(() => result.current.prepare())
    await act(async () => {
      release({
        status: 'success',
        evaluation: { summary: 'late' },
        checks: { package_match: true, label_conflicts: [], warnings: [] },
      })
      await pending
    })
    expect(result.current.imported).toBeNull()
    expect(result.current.failure).toBeNull()
  })

  it('marks an analysis checked against an earlier selection', async () => {
    validateEvaluation.mockResolvedValue({
      status: 'success',
      evaluation: { summary: 's' },
      checks: { package_match: true, label_conflicts: [], warnings: [] },
    })
    const { result, rerender } = renderHook((p) => useStrategyEvaluation(p), {
      wrapper,
      initialProps: base,
    })
    await act(() => result.current.prepare())
    await act(() => result.current.validate('pasted'))
    expect(result.current.importedEarlier).toBe(false)
    rerender({ ...base, capital: 5000 as never })
    expect(result.current.importedEarlier).toBe(true)
    rerender(base)
    expect(result.current.importedEarlier).toBe(false)
  })
})
