import { useEffect, useId, useRef, useState } from 'react'
import { fmtStamp, type NetBar, type ScatterPoint, signedMoney } from '@/lib/discretionaryPnlView'
import { cn } from '@/lib/utils'

type Money = (value: number) => string

/**
 * Net P&L bars: green to the right for a gain, red to the left for a loss, and the sign in the
 * value label, so the direction never rests on colour alone. Plain markup, no chart library.
 */
export function NetBarChart({
  title,
  bars,
  formatCurrency,
  samples,
  emptyText,
  maxRows = 12,
  testId,
}: {
  title: string
  bars: NetBar[]
  formatCurrency: Money
  /** Describes what a bar's count is, e.g. "positions". */
  samples: string
  emptyText: string
  maxRows?: number
  testId: string
}) {
  const shown =
    bars.length > maxRows
      ? [...bars].sort((a, b) => Math.abs(b.value) - Math.abs(a.value)).slice(0, maxRows)
      : bars
  const ordered = bars.length > maxRows ? bars.filter((b) => shown.includes(b)) : bars
  return (
    <div className="min-w-0" data-testid={testId}>
      <div className="mb-1 text-sm font-medium">{title}</div>
      {ordered.length === 0 ? (
        <p className="text-sm text-muted-foreground">{emptyText}</p>
      ) : (
        <>
          <ul
            className="space-y-1"
            aria-label={`${title}, net P&L in rupees (Rs) after estimated charges`}
          >
            {ordered.map((b) => (
              <li
                key={b.key}
                className="grid grid-cols-[4.5rem_1fr_7.5rem] items-center gap-2 text-xs sm:grid-cols-[9rem_1fr_7.5rem]"
                title={`${b.label}: net ${signedMoney(b.value, formatCurrency)} from ${b.count} ${samples}`}
                data-testid="net-bar"
              >
                <span className="truncate font-medium">{b.label}</span>
                <span className="relative h-4 border-x border-dashed border-border">
                  <span className="absolute inset-y-0 left-1/2 w-px bg-border" />
                  {b.value !== 0 && (
                    <span
                      className={cn(
                        'absolute inset-y-0.5 rounded-sm',
                        b.value > 0 ? 'left-1/2 bg-green-600/80' : 'right-1/2 bg-red-600/80'
                      )}
                      style={{ width: `${Math.max(b.pct, 2) / 2}%` }}
                    />
                  )}
                </span>
                <span
                  className={cn(
                    'text-right font-mono',
                    b.value > 0 && 'text-green-600',
                    b.value < 0 && 'text-red-600'
                  )}
                >
                  {signedMoney(b.value, formatCurrency)}
                </span>
              </li>
            ))}
          </ul>
          <p className="mt-1 text-[11px] text-muted-foreground">
            Net P&amp;L in rupees (Rs), after estimated charges; green to the right is a gain, red
            to the left a loss.{' '}
            {bars.length > maxRows
              ? `Showing the ${maxRows} largest of ${bars.length}.`
              : `${bars.length} shown.`}
          </p>
        </>
      )}
    </div>
  )
}

const PAD = { left: 56, right: 16, top: 14, bottom: 48 }
const TICKS = [0, 0.25, 0.5, 0.75, 1]

function niceMax(v: number): number {
  if (v <= 0) return 1
  const pow = 10 ** Math.floor(Math.log10(v))
  const n = v / pow
  const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10
  return step * pow
}

function kindWord(kind: ScatterPoint['kind']): string {
  return kind === 'winner' ? 'winner' : kind === 'loser' ? 'loser' : 'break-even or no net'
}

/** One marker: circle (winner), diamond (loser) or square (break-even); focusable. */
function Marker({
  p,
  cx,
  cy,
  active,
  onActive,
  label,
}: {
  p: ScatterPoint
  cx: number
  cy: number
  active: boolean
  onActive: (id: number | null) => void
  label: string
}) {
  const common = {
    tabIndex: 0,
    role: 'img' as const,
    'aria-label': label,
    onMouseEnter: () => onActive(p.id),
    onMouseLeave: () => onActive(null),
    onFocus: () => onActive(p.id),
    onBlur: () => onActive(null),
    fillOpacity: 0.85,
    stroke: active ? 'currentColor' : 'none',
    strokeWidth: 2,
    className: 'cursor-pointer outline-none',
  }
  if (p.kind === 'winner') {
    return <circle cx={cx} cy={cy} r={6} fill="#16a34a" data-testid="scatter-winner" {...common} />
  }
  if (p.kind === 'loser') {
    return (
      <polygon
        points={`${cx},${cy - 8} ${cx + 8},${cy} ${cx},${cy + 8} ${cx - 8},${cy}`}
        fill="#dc2626"
        data-testid="scatter-loser"
        {...common}
      />
    )
  }
  return (
    <rect
      x={cx - 5}
      y={cy - 5}
      width={10}
      height={10}
      fill="#6b7280"
      data-testid="scatter-even"
      {...common}
    />
  )
}

/**
 * MAE against MFE of the closed positions, in points per unit. A winner is a green circle, a
 * loser a red diamond and a break-even a grey square, so the kind never rests on colour alone.
 * Every marker takes keyboard focus and shows its details in a tooltip; a table lists the same
 * values.
 */
export function MaeMfeScatter({
  points,
  formatCurrency,
}: {
  points: ScatterPoint[]
  formatCurrency?: Money
}) {
  const titleId = useId()
  const [activeId, setActiveId] = useState<number | null>(null)
  // Drawn at the card's real width (so text and markers keep their size on a phone too).
  const boxRef = useRef<HTMLDivElement>(null)
  const [W, setW] = useState(720)
  useEffect(() => {
    const el = boxRef.current
    if (!el) return
    const measure = () => {
      if (el.clientWidth > 0) setW(Math.max(el.clientWidth, 280))
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  const H = Math.round(Math.min(380, Math.max(260, W * 0.42)))
  if (points.length === 0) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="mae-mfe-scatter-empty">
        No position has MAE / MFE yet, so there is nothing to plot.
      </p>
    )
  }
  const money = formatCurrency ?? ((v: number) => v.toFixed(2))
  const maxX = niceMax(Math.max(...points.map((p) => p.mae)))
  const maxY = niceMax(Math.max(...points.map((p) => p.mfe)))
  const x = (v: number) => PAD.left + (v / maxX) * (W - PAD.left - PAD.right)
  const y = (v: number) => H - PAD.bottom - (v / maxY) * (H - PAD.top - PAD.bottom)
  const counts = {
    winner: points.filter((p) => p.kind === 'winner').length,
    loser: points.filter((p) => p.kind === 'loser').length,
    even: points.filter((p) => p.kind === 'even').length,
  }
  const active = points.find((p) => p.id === activeId) ?? null
  const detail = (p: ScatterPoint) =>
    `${p.symbol}${p.direction ? ` ${p.direction === 'LONG' ? 'Long' : 'Short'}` : ''}: ${kindWord(p.kind)}, MAE ${p.mae.toFixed(2)}, MFE ${p.mfe.toFixed(2)} points per unit${p.net != null ? `, net ${money(p.net)}` : ''}`
  return (
    <div data-testid="mae-mfe-scatter" ref={boxRef} className="relative min-w-0">
      <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" aria-labelledby={titleId}>
        <title id={titleId}>
          MAE against MFE in points per unit: {counts.winner} winners, {counts.loser} losers
        </title>
        {TICKS.map((t) => (
          <g key={t} className="text-muted-foreground">
            <line
              x1={PAD.left}
              x2={W - PAD.right}
              y1={y(t * maxY)}
              y2={y(t * maxY)}
              stroke="currentColor"
              strokeOpacity={t === 0 ? 0.6 : 0.18}
            />
            <line
              x1={x(t * maxX)}
              x2={x(t * maxX)}
              y1={PAD.top}
              y2={H - PAD.bottom}
              stroke="currentColor"
              strokeOpacity={t === 0 ? 0.6 : 0.1}
            />
            <text
              x={PAD.left - 6}
              y={y(t * maxY) + 4}
              fontSize="12"
              textAnchor="end"
              fill="currentColor"
            >
              {+(t * maxY).toFixed(2)}
            </text>
            <text
              x={x(t * maxX)}
              y={H - PAD.bottom + 18}
              fontSize="12"
              textAnchor="middle"
              fill="currentColor"
            >
              {+(t * maxX).toFixed(2)}
            </text>
          </g>
        ))}
        <text
          x={(PAD.left + W - PAD.right) / 2}
          y={H - 8}
          fontSize="12"
          textAnchor="middle"
          fill="currentColor"
        >
          MAE (points per unit)
        </text>
        <text
          x={14}
          y={(PAD.top + H - PAD.bottom) / 2}
          fontSize="12"
          textAnchor="middle"
          fill="currentColor"
          transform={`rotate(-90 14 ${(PAD.top + H - PAD.bottom) / 2})`}
        >
          MFE (points per unit)
        </text>
        {points.map((p) => (
          <Marker
            key={p.id}
            p={p}
            cx={x(p.mae)}
            cy={y(p.mfe)}
            active={p.id === activeId}
            onActive={setActiveId}
            label={detail(p)}
          />
        ))}
      </svg>
      {active && (
        <output
          className="pointer-events-none absolute z-10 w-56 rounded-md border bg-popover p-2 text-xs text-popover-foreground shadow-md"
          style={{
            left: `${Math.min(Math.max((x(active.mae) / W) * 100, 2), 70)}%`,
            top: `${Math.max((y(active.mfe) / H) * 100 - 6, 0)}%`,
          }}
          data-testid="scatter-tooltip"
        >
          <div className="font-medium">
            {active.symbol}
            {active.direction ? ` ${active.direction === 'LONG' ? 'Long' : 'Short'}` : ''}
          </div>
          <div>Entry {fmtStamp(active.opened_at)}</div>
          <div>Exit {fmtStamp(active.closed_at)}</div>
          <div>Net P&amp;L: {active.net != null ? money(active.net) : '—'}</div>
          <div>MAE {active.mae.toFixed(2)} points per unit</div>
          <div>MFE {active.mfe.toFixed(2)} points per unit</div>
        </output>
      )}
      <ul
        className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground"
        data-testid="scatter-legend"
      >
        <li>
          <span className="text-green-600">Green circle</span>: winner ({counts.winner})
        </li>
        <li>
          <span className="text-red-600">Red diamond</span>: loser ({counts.loser})
        </li>
        {counts.even > 0 && <li>Grey square: break-even or no net ({counts.even})</li>}
      </ul>
      <details className="mt-2 text-xs" data-testid="scatter-table">
        <summary className="cursor-pointer text-muted-foreground">
          Show the {points.length} plotted position(s) as a table
        </summary>
        <div className="mt-1 max-h-64 overflow-auto rounded border">
          <table className="w-full min-w-[36rem] text-left">
            <thead className="sticky top-0 bg-muted">
              <tr>
                <th className="px-2 py-1">Symbol</th>
                <th className="px-2 py-1">Result</th>
                <th className="px-2 py-1">Entry</th>
                <th className="px-2 py-1">Exit</th>
                <th className="px-2 py-1 text-right">Net P&amp;L</th>
                <th className="px-2 py-1 text-right">MAE</th>
                <th className="px-2 py-1 text-right">MFE</th>
              </tr>
            </thead>
            <tbody>
              {points.map((p) => (
                <tr key={p.id} className="border-t">
                  <td className="px-2 py-1">
                    {p.symbol}
                    {p.direction ? ` ${p.direction === 'LONG' ? 'Long' : 'Short'}` : ''}
                  </td>
                  <td className="px-2 py-1">{kindWord(p.kind)}</td>
                  <td className="px-2 py-1">{fmtStamp(p.opened_at)}</td>
                  <td className="px-2 py-1">{fmtStamp(p.closed_at)}</td>
                  <td className="px-2 py-1 text-right font-mono">
                    {p.net != null ? money(p.net) : '—'}
                  </td>
                  <td className="px-2 py-1 text-right font-mono">{p.mae.toFixed(2)}</td>
                  <td className="px-2 py-1 text-right font-mono">{p.mfe.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  )
}
