import { useId } from 'react'
import type { ScatterPoint } from '@/lib/discretionaryPnlView'
import { type NetBar, signedMoney } from '@/lib/discretionaryPnlView'
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

const W = 420
const H = 280
const PAD = { left: 48, right: 12, top: 12, bottom: 40 }

function niceMax(v: number): number {
  if (v <= 0) return 1
  const pow = 10 ** Math.floor(Math.log10(v))
  const n = v / pow
  const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10
  return step * pow
}

/**
 * MAE against MFE of the closed positions, in points per unit. A winner is a green circle, a
 * loser a red diamond and a break-even a grey square, so the kind never rests on colour alone.
 */
export function MaeMfeScatter({ points }: { points: ScatterPoint[] }) {
  const titleId = useId()
  if (points.length === 0) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="mae-mfe-scatter-empty">
        No position has MAE / MFE yet, so there is nothing to plot.
      </p>
    )
  }
  const maxX = niceMax(Math.max(...points.map((p) => p.mae)))
  const maxY = niceMax(Math.max(...points.map((p) => p.mfe)))
  const x = (v: number) => PAD.left + (v / maxX) * (W - PAD.left - PAD.right)
  const y = (v: number) => H - PAD.bottom - (v / maxY) * (H - PAD.top - PAD.bottom)
  const counts = {
    winner: points.filter((p) => p.kind === 'winner').length,
    loser: points.filter((p) => p.kind === 'loser').length,
    even: points.filter((p) => p.kind === 'even').length,
  }
  const ticks = [0, 0.5, 1]
  return (
    <div data-testid="mae-mfe-scatter" className="min-w-0">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="h-auto w-full max-w-[520px]"
        role="img"
        aria-labelledby={titleId}
      >
        <title id={titleId}>
          MAE against MFE in points per unit: {counts.winner} winners, {counts.loser} losers
        </title>
        {ticks.map((t) => (
          <g key={t} className="text-muted-foreground">
            <line
              x1={PAD.left}
              x2={W - PAD.right}
              y1={y(t * maxY)}
              y2={y(t * maxY)}
              stroke="currentColor"
              strokeOpacity={0.2}
            />
            <text
              x={PAD.left - 6}
              y={y(t * maxY) + 3}
              fontSize="10"
              textAnchor="end"
              fill="currentColor"
            >
              {+(t * maxY).toFixed(2)}
            </text>
            <text
              x={x(t * maxX)}
              y={H - PAD.bottom + 14}
              fontSize="10"
              textAnchor="middle"
              fill="currentColor"
            >
              {+(t * maxX).toFixed(2)}
            </text>
          </g>
        ))}
        <line
          x1={PAD.left}
          x2={PAD.left}
          y1={PAD.top}
          y2={H - PAD.bottom}
          stroke="currentColor"
          strokeOpacity={0.4}
        />
        <line
          x1={PAD.left}
          x2={W - PAD.right}
          y1={H - PAD.bottom}
          y2={H - PAD.bottom}
          stroke="currentColor"
          strokeOpacity={0.4}
        />
        <text
          x={(PAD.left + W - PAD.right) / 2}
          y={H - 6}
          fontSize="11"
          textAnchor="middle"
          fill="currentColor"
        >
          MAE: worst move against you (points per unit)
        </text>
        <text
          x={12}
          y={(PAD.top + H - PAD.bottom) / 2}
          fontSize="11"
          textAnchor="middle"
          fill="currentColor"
          transform={`rotate(-90 12 ${(PAD.top + H - PAD.bottom) / 2})`}
        >
          MFE: best move for you (points)
        </text>
        {points.map((p) => {
          const cx = x(p.mae)
          const cy = y(p.mfe)
          const label = `${p.symbol}: MAE ${p.mae.toFixed(2)}, MFE ${p.mfe.toFixed(2)} points; ${
            p.kind === 'winner' ? 'winner' : p.kind === 'loser' ? 'loser' : 'break-even'
          }`
          if (p.kind === 'winner') {
            return (
              <circle
                key={p.id}
                cx={cx}
                cy={cy}
                r={4}
                fill="#16a34a"
                fillOpacity={0.8}
                data-testid="scatter-winner"
              >
                <title>{label}</title>
              </circle>
            )
          }
          if (p.kind === 'loser') {
            return (
              <polygon
                key={p.id}
                points={`${cx},${cy - 5} ${cx + 5},${cy} ${cx},${cy + 5} ${cx - 5},${cy}`}
                fill="#dc2626"
                fillOpacity={0.8}
                data-testid="scatter-loser"
              >
                <title>{label}</title>
              </polygon>
            )
          }
          return (
            <rect
              key={p.id}
              x={cx - 3.5}
              y={cy - 3.5}
              width={7}
              height={7}
              fill="#6b7280"
              fillOpacity={0.8}
              data-testid="scatter-even"
            >
              <title>{label}</title>
            </rect>
          )
        })}
      </svg>
      <ul className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <li>
          <span className="text-green-600">Circle</span>: winner ({counts.winner})
        </li>
        <li>
          <span className="text-red-600">Diamond</span>: loser ({counts.loser})
        </li>
        {counts.even > 0 && <li>Square: break-even or no net ({counts.even})</li>}
      </ul>
    </div>
  )
}
