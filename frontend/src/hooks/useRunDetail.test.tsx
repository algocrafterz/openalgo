import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/backtestResults', () => ({
  backtestResultsApi: { run: vi.fn() },
}))

import { backtestResultsApi } from '@/api/backtestResults'
import { useRunDetail } from './useRunDetail'

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
}

describe('useRunDetail', () => {
  beforeEach(() => {
    vi.mocked(backtestResultsApi.run).mockReset()
  })

  it('does not fetch without ids', () => {
    renderHook(() => useRunDetail('', 'r1'), { wrapper: wrapper() })
    renderHook(() => useRunDetail('idea', ''), { wrapper: wrapper() })
    renderHook(() => useRunDetail(undefined, undefined), { wrapper: wrapper() })
    expect(backtestResultsApi.run).not.toHaveBeenCalled()
  })

  it('fetches when both ids are set', async () => {
    vi.mocked(backtestResultsApi.run).mockResolvedValue({
      status: 'success',
      run: { run_id: 'r1' },
    } as never)
    const { result } = renderHook(() => useRunDetail('idea', 'r1'), { wrapper: wrapper() })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(backtestResultsApi.run).toHaveBeenCalledWith('idea', 'r1')
  })
})
