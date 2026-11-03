/** Today in IST as YYYY-MM-DD - the platform trades Indian markets only. */
export function todayIst(): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Kolkata' }).format(new Date())
}

/** "09:00-10:00" for an exit-hour bucket. */
export function hourLabel(hour: number): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(hour)}:00-${pad((hour + 1) % 24)}:00`
}

/** "42m", "2h 05m" or "1d 3h" for a holding time in minutes; an em dash when unknown. */
export function fmtDuration(min: number | null | undefined): string {
  if (min == null || Number.isNaN(min)) return '—'
  const total = Math.max(0, Math.round(min))
  if (total < 60) return `${total}m`
  if (total < 1440) {
    return `${Math.floor(total / 60)}h ${String(total % 60).padStart(2, '0')}m`
  }
  return `${Math.floor(total / 1440)}d ${Math.floor((total % 1440) / 60)}h`
}

/** "30 Oct 2026" from YYYY-MM-DD; the input when it does not parse. */
export function fmtDate(iso: string | undefined): string {
  if (!iso) return ''
  const d = new Date(`${iso}T00:00:00`)
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' })
}

export function fmtR(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `${value > 0 ? '+' : ''}${value.toFixed(2)}R`
}

export function pnlClass(value: number): string {
  return value > 0 ? 'text-green-600' : value < 0 ? 'text-red-600' : ''
}

/** Plain-words reasons a trade has no R multiple, with counts. */
const R_REASON_TEXT: Record<string, string> = {
  no_signal_data: 'no signal records found',
  no_unique_signal_match: 'no single matching signal stop',
  zero_stop_distance: 'stop equal to entry',
}

export function rExcludedText(excluded: Record<string, number> | undefined): string | undefined {
  const parts = Object.entries(excluded ?? {})
    .filter(([, n]) => n > 0)
    .map(([reason, n]) => `${n} ${R_REASON_TEXT[reason] ?? reason}`)
  return parts.length ? `No R: ${parts.join(', ')}` : undefined
}

/** "N trades" or "M of N trades" for the R-multiple coverage. */
export function rCoverage(d: { r_covered?: number; r_total?: number }): string | undefined {
  if (!d.r_total) return undefined
  return d.r_covered === d.r_total ? `${d.r_total} trades` : `${d.r_covered} of ${d.r_total} trades`
}

/** "charges Rs X (Y% of gross profit)" - how much of the profit the charges take. */
export function chargesHint(
  d: {
    est_costs?: number | null
    charges_pct_of_gross?: number | null
    fee_basis_assumed?: boolean
  },
  format: (value: number) => string
): string | undefined {
  if (d.est_costs == null) return undefined
  const amount = format(d.est_costs)
  const note = d.fee_basis_assumed ? ' [assumed broker rates]' : ''
  if (d.charges_pct_of_gross == null) return `charges ${amount} (no gross profit)${note}`
  return `charges ${amount} = ${d.charges_pct_of_gross.toFixed(1)}% of gross profit${note}`
}

/** "measured from N of M trades" - slippage is already in the P&L, so it is information only. */
export function slippageHint(d: {
  slippage_covered?: number
  slippage_total?: number
}): string | undefined {
  if (!d.slippage_total) return undefined
  return `already in P&L, measured on ${d.slippage_covered ?? 0} of ${d.slippage_total} trades`
}

/** Display word for a benchmark grade. */
export function gradeLabel(grade: string | null | undefined): string {
  switch (grade) {
    case 'poor':
      return 'Poor'
    case 'acceptable':
      return 'Acceptable'
    case 'good':
      return 'Good'
    case 'excellent':
      return 'Excellent'
    default:
      return 'Not graded'
  }
}

/** Short table label for an edge verdict. */
export function edgeShortLabel(verdict: string | undefined): string {
  switch (verdict) {
    case 'positive_supported':
      return 'Supported'
    case 'positive_inconclusive':
      return 'Positive, unproven'
    case 'negative':
      return 'Negative'
    case 'inconclusive':
      return 'Unclear'
    default:
      return 'Too few trades'
  }
}

/** Plain-words verdict sentence for the Edge check card. */
export function edgeVerdictText(verdict: string | undefined): string {
  switch (verdict) {
    case 'positive_supported':
      return 'Positive and statistically supported'
    case 'positive_inconclusive':
      return 'Positive but statistically inconclusive'
    case 'negative':
      return 'Negative expectancy'
    case 'inconclusive':
      return 'Inconclusive, not shown to be positive'
    default:
      return 'Insufficient sample'
  }
}
