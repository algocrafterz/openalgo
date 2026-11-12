import { describe, expect, it } from 'vitest'
import { recentlyEnded, remedyFor } from './workflowRemedies'

describe('remedyFor', () => {
  it('maps a failed close-day step to the action of the same name', () => {
    for (const name of [
      'backfill',
      'eod',
      'execution_health',
      'logs',
      'value_areas',
      'jobs',
      'review',
    ]) {
      expect(remedyFor({ phase: 'close-day', group: 'step', name })).toBe(name)
    }
  })

  it('maps close-day check results to the step that rebuilds them', () => {
    expect(remedyFor({ phase: 'close-day', group: 'logs', name: 'RISING x36' })).toBe('logs')
    expect(remedyFor({ phase: 'close-day', group: 'value-areas', name: 'coverage' })).toBe(
      'value_areas'
    )
    expect(remedyFor({ phase: 'close-day', group: 'jobs', name: 'phase close-day' })).toBe('jobs')
  })

  it('offers nothing for the morning checks, the engine, the notify step or execution metrics', () => {
    expect(
      remedyFor({ phase: 'liveness', group: 'liveness', name: 'first runner cycle' })
    ).toBeNull()
    expect(remedyFor({ phase: 'open-day', group: 'open-day', name: 'engine startup' })).toBeNull()
    expect(remedyFor({ phase: 'close-day', group: 'step', name: 'notify' })).toBeNull()
    expect(remedyFor({ phase: 'close-day', group: 'execution', name: 'Order timeouts' })).toBeNull()
  })
})

describe('recentlyEnded', () => {
  const now = Date.parse('2026-11-12T18:00:00+05:30')
  it('is true within an hour of the end and false after, or without an end', () => {
    expect(recentlyEnded('2026-11-12T17:30:00+05:30', now)).toBe(true)
    expect(recentlyEnded('2026-11-12T16:30:00+05:30', now)).toBe(false)
    expect(recentlyEnded(undefined, now)).toBe(false)
    expect(recentlyEnded('garbage', now)).toBe(false)
  })
})
