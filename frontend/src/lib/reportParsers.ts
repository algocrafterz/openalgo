/**
 * Readers for the markdown reports the daily phases write. They only lift out what is printed in
 * the report. A missing table, row or column gives null / undefined, never a guessed value.
 */

export type MetricStatus = 'OK' | 'WATCH' | 'REGRESSION' | string

export interface ExecutionMetric {
  metric: string
  today: string
  status: MetricStatus
  prev?: string
}

export interface ExecutionHealth {
  overall?: string
  rows: ExecutionMetric[]
}

export interface StrategyPnl {
  name: string
  gross: number
  charges: number
  net: number
}

export interface StrategyPnlSummary {
  strategies: StrategyPnl[]
  total?: StrategyPnl
  /** Total trades across all strategies, only when the report prints it. */
  trades?: number
  /** Win rate across all strategies as printed (percent), only when the report prints it. */
  winRatePct?: number
  /** PASS or FAIL as printed by the pages-vs-fills check. */
  reconciliation?: string
}

function cells(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((c) => c.trim())
}

export function parseExecutionHealth(md: string): ExecutionHealth | null {
  const lines = md.split(/\r?\n/)
  const head = lines.findIndex((l) => /^\|\s*Metric\s*\|/i.test(l))
  if (head < 0) return null
  const columns = cells(lines[head]).map((c) => c.toLowerCase())
  const at = (name: RegExp) => columns.findIndex((c) => name.test(c))
  const iMetric = at(/^metric$/)
  const iToday = at(/^today$/)
  const iStatus = at(/^status$/)
  const iPrev = at(/^prev/)
  if (iMetric < 0 || iToday < 0 || iStatus < 0) return null

  const rows: ExecutionMetric[] = []
  for (const line of lines.slice(head + 1)) {
    if (!line.trim().startsWith('|')) break
    if (/^\|\s*-+/.test(line)) continue
    const c = cells(line)
    if (c.length <= Math.max(iMetric, iToday, iStatus)) continue
    rows.push({
      metric: c[iMetric],
      today: c[iToday],
      status: c[iStatus],
      prev: iPrev >= 0 ? c[iPrev] : undefined,
    })
  }
  if (rows.length === 0) return null
  const overall = md.match(/^Overall:\s*\*\*([A-Z ]+)\*\*/m)?.[1]?.trim()
  return { overall, rows }
}

function num(text: string): number {
  return Number(text.replace(/,/g, ''))
}

const CHARGES_ROW =
  /^\s*(.+?)\s+gross\s+(-?[\d,]+(?:\.\d+)?)\s+charges\s+(-?[\d,]+(?:\.\d+)?)\b.*?after charges\s+(-?[\d,]+(?:\.\d+)?)\s*$/

export function parseStrategyPnl(md: string): StrategyPnlSummary | null {
  const strategies: StrategyPnl[] = []
  let total: StrategyPnl | undefined
  for (const line of md.split(/\r?\n/)) {
    const m = CHARGES_ROW.exec(line)
    if (!m) continue
    const row = { name: m[1], gross: num(m[2]), charges: num(m[3]), net: num(m[4]) }
    if (/^ALL STRATEGIES$/i.test(row.name)) total = row
    else strategies.push(row)
  }
  if (strategies.length === 0 && !total) return null
  const trades = md.match(/:\s*(\d+)\s+trades,\s+charges/)?.[1]
  const win = md.match(/realised R[^\n]*\bwin\s+(-?[\d.]+)%/)?.[1]
  const result = md.match(/^RESULT:\s*([A-Z]+)/m)?.[1]
  return {
    strategies,
    total,
    trades: trades ? Number(trades) : undefined,
    winRatePct: win ? Number(win) : undefined,
    reconciliation: result,
  }
}
