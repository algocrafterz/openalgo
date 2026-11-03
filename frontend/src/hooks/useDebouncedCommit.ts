import { useCallback, useEffect, useRef } from 'react'

/**
 * Delays a commit until typing pauses. `schedule(text)` restarts the wait,
 * `flush(text)` commits at once (blur, Enter, cleared field) and `cancel()` drops
 * a pending commit. `isPending()` is true while a commit is scheduled. A pending
 * commit is also dropped on unmount.
 */
export function useDebouncedCommit(onCommit: (text: string) => void, delayMs: number) {
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const latest = useRef(onCommit)
  latest.current = onCommit

  const cancel = useCallback(() => {
    if (timer.current !== undefined) {
      clearTimeout(timer.current)
      timer.current = undefined
    }
  }, [])

  const isPending = useCallback(() => timer.current !== undefined, [])

  const schedule = useCallback(
    (text: string) => {
      cancel()
      timer.current = setTimeout(() => {
        timer.current = undefined
        latest.current(text)
      }, delayMs)
    },
    [cancel, delayMs]
  )

  const flush = useCallback(
    (text: string) => {
      cancel()
      latest.current(text)
    },
    [cancel]
  )

  useEffect(() => cancel, [cancel])

  return { schedule, flush, cancel, isPending }
}
