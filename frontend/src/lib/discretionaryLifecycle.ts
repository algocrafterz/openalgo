import type { TradeFill } from '@/api/discretionaryPnl'

/**
 * Position lifecycle of ONE completed trade, built from the fills the trade endpoint returns.
 *
 * Fills are put in the engine's order (fill_time, order_id, fill_id, fill_ref), consecutive
 * fills of one broker order are folded into one event with a quantity-weighted price, and the
 * running net position (sum of side x qty_used) is carried through. `qty_used` of a fill that
 * reverses a position covers only this trade's part, so a trade of a reversal shows just its
 * own portion and its running position never crosses zero.
 */

export type LifecycleLabel = 'Entry' | 'Addition' | 'Partial exit' | 'Final exit'

/** The fields of a fill the lifecycle needs. */
export type LifecycleFill = Pick<
  TradeFill,
  'side' | 'qty_used' | 'price' | 'fill_time' | 'order_id' | 'fill_id' | 'fill_ref'
> &
  Partial<Pick<TradeFill, 'charge_allocated' | 'role' | 'attribution_state'>>

export interface LifecycleEvent<F extends LifecycleFill = LifecycleFill> {
  /** Stable key: the first fill's ref. */
  key: string
  label: LifecycleLabel
  side: 'BUY' | 'SELL'
  /** Order id the fills belong to (empty when the broker gave none). */
  orderId: string
  /** Time of the first fill and of the last (the same when one fill). */
  startTime: string | null
  endTime: string | null
  /** Sum of qty_used of the fills of this event. */
  quantity: number
  /** Quantity-weighted average price of those fills. */
  price: number
  /** Net position after the event: positive long, negative short, 0 flat. */
  netAfter: number
  /** The fills, in order. More than one means a partly-filled order. */
  fills: F[]
  /** Estimated charges of the event; null when any of its fills has no estimate. */
  charges: number | null
}

export interface Lifecycle<F extends LifecycleFill = LifecycleFill> {
  events: LifecycleEvent<F>[]
  /** True when the fills open from flat, never cross zero and end flat. */
  complete: boolean
  /** Highest absolute running net; only set when `complete` (the fills list is whole). */
  peakPosition: number | null
  bought: number
  sold: number
}

function sideSign(side: string): 1 | -1 {
  return side.toUpperCase() === 'SELL' ? -1 : 1
}

/** The engine's order: (fill_time, order_id, fill_id or '', id). */
export function compareFills(a: LifecycleFill, b: LifecycleFill): number {
  const cmp = (x: string, y: string) => (x < y ? -1 : x > y ? 1 : 0)
  return (
    cmp(a.fill_time ?? '', b.fill_time ?? '') ||
    cmp(String(a.order_id ?? ''), String(b.order_id ?? '')) ||
    cmp(String(a.fill_id ?? ''), String(b.fill_id ?? '')) ||
    (a.fill_ref ?? 0) - (b.fill_ref ?? 0)
  )
}

const EPS = 1e-9

export function buildLifecycle<F extends LifecycleFill>(fills: readonly F[]): Lifecycle<F> {
  const sorted = [...fills].sort(compareFills)
  const groups: F[][] = []
  for (const fill of sorted) {
    const last = groups[groups.length - 1]
    const orderId = String(fill.order_id ?? '')
    if (
      last &&
      orderId !== '' &&
      String(last[0].order_id ?? '') === orderId &&
      last[0].side.toUpperCase() === fill.side.toUpperCase()
    ) {
      last.push(fill)
    } else {
      groups.push([fill])
    }
  }

  const events: LifecycleEvent<F>[] = []
  let net = 0
  let complete = groups.length > 0
  let peak = 0
  let bought = 0
  let sold = 0
  for (const group of groups) {
    const sign = sideSign(group[0].side)
    const quantity = group.reduce((s, f) => s + f.qty_used, 0)
    const notional = group.reduce((s, f) => s + f.qty_used * f.price, 0)
    const before = net
    net += sign * quantity
    if (Math.abs(net) < EPS) net = 0
    if (sign > 0) bought += quantity
    else sold += quantity

    let label: LifecycleLabel
    if (events.length === 0) {
      label = 'Entry'
      if (Math.abs(before) > EPS) complete = false
    } else if (Math.sign(before) === sign) {
      label = 'Addition'
    } else if (net === 0) {
      label = 'Final exit'
    } else {
      label = 'Partial exit'
      // a reduction must stay on the same side of zero; crossing it means the list is not whole
      if (Math.sign(net) !== Math.sign(before)) complete = false
    }
    if (events.length > 0 && before === 0) complete = false
    peak = Math.max(peak, Math.abs(net))

    const charged = group.map((f) => f.charge_allocated)
    events.push({
      key: String(group[0].fill_ref),
      label,
      side: sign > 0 ? 'BUY' : 'SELL',
      orderId: String(group[0].order_id ?? ''),
      startTime: group[0].fill_time,
      endTime: group[group.length - 1].fill_time,
      quantity,
      price: quantity > 0 ? notional / quantity : group[0].price,
      netAfter: net,
      fills: group,
      charges: charged.some((c) => c == null)
        ? null
        : charged.reduce<number>((s, c) => s + (c as number), 0),
    })
  }
  if (net !== 0) complete = false
  return {
    events,
    complete,
    peakPosition: complete ? peak : null,
    bought,
    sold,
  }
}

/** "+25", "-5", "0": the net position as the table shows it. */
export function fmtNet(net: number): string {
  if (net === 0) return '0'
  const text = Number.isInteger(net)
    ? String(Math.abs(net))
    : String(Number(Math.abs(net).toFixed(4)))
  return `${net > 0 ? '+' : '-'}${text}`
}
