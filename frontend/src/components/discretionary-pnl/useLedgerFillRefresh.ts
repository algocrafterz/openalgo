import { useEffect, useRef } from 'react'
import { useSocketContext } from '@/components/socket/SocketProvider'

/**
 * Refetch delays after a live fill's `order_update` socket event (ms after the LAST event).
 *
 * The server does not write the fill when the event arrives: the discretionary ledger subscriber
 * waits DISCRETIONARY_LEDGER_EVENT_DEBOUNCE_SEC (default 8 s) and then reads the broker TradeBook
 * (production sync runs: p50 0.12 s, p95 3.2 s). A refetch earlier than about 8 s cannot show the
 * fill, so the first one is at 10 s (typical sync done) and the second at 25 s (a slow TradeBook
 * read, or the server's trailing re-check: 8 s + sync + 8 s + sync). Every new event restarts both
 * timers, so a burst of partial fills causes one pair of refetches. The 60 s poll still covers a
 * missed event.
 */
export const LEDGER_FILL_REFRESH_MS: readonly number[] = [10_000, 25_000]

/** Socket.IO event emitted by subscribers/socketio_subscriber.on_order_update. */
export const ORDER_UPDATE_EVENT = 'order_update'

/** A broker (live) order update with some quantity filled: the only kind the ledger syncs on. */
export function isLiveFill(payload: unknown): boolean {
  if (!payload || typeof payload !== 'object') return false
  const p = payload as { mode?: unknown; filled_quantity?: unknown }
  return p.mode === 'live' && Number(p.filled_quantity) > 0
}

/**
 * Calls `refresh` LEDGER_FILL_REFRESH_MS after the last live fill seen on the app-wide socket.
 * Listens only while mounted; pending timers are cleared on unmount. Opens no connection.
 */
export function useLedgerFillRefresh(refresh: () => void): void {
  const { socket } = useSocketContext()
  const refreshRef = useRef(refresh)
  useEffect(() => {
    refreshRef.current = refresh
  }, [refresh])

  useEffect(() => {
    if (!socket) return
    let timers: ReturnType<typeof setTimeout>[] = []
    const clear = () => {
      for (const t of timers) clearTimeout(t)
      timers = []
    }
    const onUpdate = (payload: unknown) => {
      if (!isLiveFill(payload)) return
      clear()
      timers = LEDGER_FILL_REFRESH_MS.map((ms) => setTimeout(() => refreshRef.current(), ms))
    }
    socket.on(ORDER_UPDATE_EVENT, onUpdate)
    return () => {
      socket.off(ORDER_UPDATE_EVENT, onUpdate)
      clear()
    }
  }, [socket])
}
