import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useDebouncedCommit } from './useDebouncedCommit'

describe('useDebouncedCommit', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('commits once, with the last text, after typing pauses', () => {
    const onCommit = vi.fn()
    const { result } = renderHook(() => useDebouncedCommit(onCommit, 500))
    for (const t of ['1', '10', '100', '1000']) {
      act(() => result.current.schedule(t))
      act(() => {
        vi.advanceTimersByTime(200)
      })
    }
    expect(onCommit).not.toHaveBeenCalled()
    act(() => {
      vi.advanceTimersByTime(500)
    })
    expect(onCommit).toHaveBeenCalledTimes(1)
    expect(onCommit).toHaveBeenCalledWith('1000')
  })

  it('flush commits at once and drops the pending timer', () => {
    const onCommit = vi.fn()
    const { result } = renderHook(() => useDebouncedCommit(onCommit, 500))
    act(() => result.current.schedule('12'))
    act(() => result.current.flush(''))
    expect(onCommit).toHaveBeenCalledTimes(1)
    expect(onCommit).toHaveBeenCalledWith('')
    act(() => {
      vi.advanceTimersByTime(1000)
    })
    expect(onCommit).toHaveBeenCalledTimes(1)
  })

  it('cancel and unmount drop a pending commit', () => {
    const onCommit = vi.fn()
    const { result, unmount } = renderHook(() => useDebouncedCommit(onCommit, 500))
    act(() => result.current.schedule('5'))
    act(() => result.current.cancel())
    act(() => {
      vi.advanceTimersByTime(1000)
    })
    expect(onCommit).not.toHaveBeenCalled()
    act(() => result.current.schedule('6'))
    unmount()
    act(() => {
      vi.advanceTimersByTime(1000)
    })
    expect(onCommit).not.toHaveBeenCalled()
  })

  it('calls the latest callback when it changes before the timer fires', () => {
    const a = vi.fn()
    const b = vi.fn()
    const { result, rerender } = renderHook(({ cb }) => useDebouncedCommit(cb, 500), {
      initialProps: { cb: a },
    })
    act(() => result.current.schedule('42'))
    rerender({ cb: b })
    act(() => {
      vi.advanceTimersByTime(500)
    })
    expect(b).toHaveBeenCalledTimes(1)
    expect(b).toHaveBeenCalledWith('42')
    expect(a).not.toHaveBeenCalled()
  })

  it('reports isPending while a commit is scheduled', () => {
    const onCommit = vi.fn()
    const { result } = renderHook(() => useDebouncedCommit(onCommit, 500))
    expect(result.current.isPending()).toBe(false)
    act(() => result.current.schedule('1'))
    expect(result.current.isPending()).toBe(true)
    act(() => {
      vi.advanceTimersByTime(500)
    })
    expect(result.current.isPending()).toBe(false)

    act(() => result.current.schedule('2'))
    expect(result.current.isPending()).toBe(true)
    act(() => result.current.flush('2'))
    expect(result.current.isPending()).toBe(false)

    act(() => result.current.schedule('3'))
    act(() => result.current.cancel())
    expect(result.current.isPending()).toBe(false)
  })

  it('keeps schedule, flush, cancel and isPending stable across rerenders', () => {
    const { result, rerender } = renderHook(({ cb }) => useDebouncedCommit(cb, 500), {
      initialProps: { cb: vi.fn() },
    })
    const first = result.current
    rerender({ cb: vi.fn() })
    expect(result.current.schedule).toBe(first.schedule)
    expect(result.current.flush).toBe(first.flush)
    expect(result.current.cancel).toBe(first.cancel)
    expect(result.current.isPending).toBe(first.isPending)
  })
})
