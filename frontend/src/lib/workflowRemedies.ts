/**
 * Which finding has a "run again" action. The server holds the real list and refuses anything else;
 * this only decides where a button is worth showing. A finding about the engine, the broker or the
 * PC has no action here: a script cannot fix those.
 */

const STEP_ACTIONS = new Set([
  'backfill',
  'eod',
  'execution_health',
  'logs',
  'value_areas',
  'jobs',
  'review',
])

/** Check groups that a close-day step produces, and the action that rebuilds them. */
const CHECK_ACTIONS: Record<string, string> = {
  logs: 'logs',
  'value-areas': 'value_areas',
  jobs: 'jobs',
}

export function remedyFor(f: { phase: string; group: string; name: string }): string | null {
  if (f.phase !== 'close-day') return null
  if (f.group === 'step') return STEP_ACTIONS.has(f.name) ? f.name : null
  return CHECK_ACTIONS[f.group] ?? null
}

/** A finished run is worth showing for an hour; after that it is just history. */
export function recentlyEnded(ended: string | undefined, now: number = Date.now()): boolean {
  if (!ended) return false
  const t = Date.parse(ended)
  return !Number.isNaN(t) && now - t < 60 * 60 * 1000
}
