interface SparklineProps {
  values: number[]
  width?: number
  height?: number
}

/** Tiny equity trend for a table row. Shape only: no axis, so it never implies a scale. */
export function Sparkline({ values, width = 90, height = 24 }: SparklineProps) {
  if (values.length < 2) return <span className="text-muted-foreground">n/a</span>
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const step = width / (values.length - 1)
  const points = values
    .map((v, i) => `${(i * step).toFixed(1)},${(height - ((v - min) / span) * height).toFixed(1)}`)
    .join(' ')
  const rising = values[values.length - 1] >= values[0]
  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={rising ? 'Equity trend up' : 'Equity trend down'}
    >
      <polyline
        points={points}
        fill="none"
        stroke={rising ? '#16a34a' : '#dc2626'}
        strokeWidth="1.5"
      />
    </svg>
  )
}
