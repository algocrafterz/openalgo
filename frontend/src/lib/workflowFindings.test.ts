import { describe, expect, it } from 'vitest'
import type { WorkflowDay } from '@/api/opsWorkflow'
import {
  buildFindings,
  categoryOf,
  countSeverities,
  defaultPhase,
  healthOf,
  metricFindings,
  phaseCompleted,
  stepFailed,
} from './workflowFindings'

function day(over: Partial<WorkflowDay> = {}): WorkflowDay {
  return {
    day: '2026-11-03',
    phases: [
      {
        phase: 'open-day',
        steps: [{ step: 'check', started: '09:12:01', ended: '09:12:30', exit: 0 }],
      },
      { phase: 'liveness', steps: null },
      { phase: 'close-day', steps: null },
    ],
    checks: {},
    reports: [],
    verdict: null,
    ...over,
  }
}

const result = (checks: { name: string; status: 'OK' | 'WARN' | 'FAIL' | 'NOT VERIFIED' }[]) => ({
  phase: 'open-day',
  day: '2026-11-03',
  verdict: 'GREEN' as const,
  summary: '',
  checks: checks.map((c) => ({ ...c, evidence: 'e', plain: '' })),
})

describe('categoryOf', () => {
  it('maps known check names and falls back to other', () => {
    expect(categoryOf('broker session (funds API)')).toBe('broker')
    expect(categoryOf('risk counters')).toBe('risk')
    expect(categoryOf('task breakingTradeAutoStart')).toBe('jobs')
    expect(categoryOf('trades.db')).toBe('infra')
    expect(categoryOf('stale prices')).toBe('data')
    expect(categoryOf('RISING x8')).toBe('other')
  })
})

describe('buildFindings', () => {
  it('skips OK checks and ranks status as severity', () => {
    const d = day({
      checks: {
        'open-day': result([
          { name: 'disk', status: 'OK' },
          { name: 'clock (NTP)', status: 'WARN' },
          { name: 'trades.db', status: 'FAIL' },
          { name: 'risk counters', status: 'NOT VERIFIED' },
        ]),
      },
    })
    const f = buildFindings(d)
    expect(f.map((x) => [x.title, x.severity])).toEqual([
      ['clock (NTP)', 'warning'],
      ['trades.db', 'critical'],
      ['risk counters', 'unverified'],
    ])
    expect(f[0].checkedAt).toBe('09:12:01')
  })
  it('turns a failed step into a critical job finding', () => {
    const d = day()
    d.phases[0].steps = [{ step: 'check', started: '09:12:01', ended: '09:12:30', exit: 124 }]
    const f = buildFindings(d)
    expect(f).toHaveLength(1)
    expect(f[0]).toMatchObject({ severity: 'critical', category: 'jobs' })
    expect(f[0].title).toContain('deadline')
  })
})

describe('metricFindings', () => {
  it('keeps WATCH and REGRESSION rows and escalates EXIT UNCERTAIN', () => {
    const f = metricFindings(
      [
        { metric: 'Limiter timeouts (total)', today: '0', status: 'OK' },
        { metric: 'Stale-price warnings (tracker)', today: '19', status: 'REGRESSION', prev: '34' },
        { metric: 'history_service errors', today: '4', status: 'WATCH' },
        { metric: 'EXIT UNCERTAIN (CRITICAL)', today: '2', status: 'REGRESSION' },
      ],
      day()
    )
    expect(f.map((x) => x.severity)).toEqual(['warning', 'info', 'critical'])
  })
})

describe('health versus completion', () => {
  it('is not healthy when a checkpoint completed but a check failed', () => {
    const d = day({ checks: { 'open-day': result([{ name: 'trades.db', status: 'FAIL' }]) } })
    expect(phaseCompleted(d, 'open-day')).toBe(true)
    expect(healthOf(d, buildFindings(d))).toBe('critical')
  })
  it('keeps an unverified check out of Healthy', () => {
    const d = day({
      checks: { 'open-day': result([{ name: 'risk counters', status: 'NOT VERIFIED' }]) },
    })
    expect(healthOf(d, buildFindings(d))).toBe('degraded')
  })
  it('is healthy only when something ran and nothing was found', () => {
    const d = day({ checks: { 'open-day': result([{ name: 'disk', status: 'OK' }]) } })
    expect(healthOf(d, buildFindings(d))).toBe('healthy')
  })
  it('has no verdict when nothing ran', () => {
    const d = day({ phases: day().phases.map((p) => ({ ...p, steps: null })) })
    expect(healthOf(d, [])).toBe('nodata')
    expect(phaseCompleted(d, 'liveness')).toBeNull()
  })
  it('counts severities', () => {
    const d = day({
      checks: {
        'open-day': result([
          { name: 'a', status: 'WARN' },
          { name: 'b', status: 'WARN' },
        ]),
      },
    })
    expect(countSeverities(buildFindings(d)).warning).toBe(2)
  })
})

describe('stepFailed', () => {
  const check = { step: 'check', started: '09:12:03', ended: '09:12:07', exit: 1 }
  it('does not call a RED check step broken: the verdict is the finding', () => {
    const d = day({
      checks: {
        'open-day': { ...result([{ name: 'trades.db', status: 'FAIL' }]), verdict: 'RED' },
      },
    })
    d.phases[0].steps = [check]
    expect(stepFailed(d, 'open-day', check)).toBe(false)
    expect(phaseCompleted(d, 'open-day')).toBe(true)
    const f = buildFindings(d)
    expect(f).toHaveLength(1)
    expect(f[0].title).toBe('trades.db')
  })
  it('still fails a check step that exited 1 with no RED result recorded', () => {
    const d = day()
    d.phases[0].steps = [check]
    expect(stepFailed(d, 'open-day', check)).toBe(true)
  })
  it('still fails a timed-out check step and any other step', () => {
    const d = day({ checks: { 'open-day': { ...result([]), verdict: 'RED' } } })
    expect(stepFailed(d, 'open-day', { ...check, exit: 124 })).toBe(true)
    expect(stepFailed(d, 'open-day', { ...check, step: 'notify' })).toBe(true)
    expect(stepFailed(d, 'close-day', check)).toBe(true)
  })
  it('treats close-day check steps the same way, but not the review or backfill', () => {
    const d = day({ checks: { jobs: { ...result([]), verdict: 'RED' } } })
    const jobs = { step: 'jobs', started: '15:43:34', ended: '15:43:35', exit: 1 }
    expect(stepFailed(d, 'close-day', jobs)).toBe(false)
    expect(stepFailed(d, 'close-day', { ...jobs, step: 'backfill' })).toBe(true)
    expect(stepFailed(d, 'close-day', { ...jobs, step: 'review' })).toBe(true)
  })
})

describe('defaultPhase', () => {
  const f = (
    phase: 'open-day' | 'liveness' | 'close-day',
    severity: 'critical' | 'warning' | 'unverified' | 'info'
  ) => ({
    id: `${phase}-${severity}-${Math.random()}`,
    severity,
    category: 'other' as const,
    title: 't',
    evidence: 'e',
    phase,
    group: 'g',
    name: 'n',
    source: 's',
  })
  it('picks the checkpoint with the most severe finding', () => {
    expect(defaultPhase([f('liveness', 'warning'), f('open-day', 'critical')])).toBe('open-day')
    expect(defaultPhase([f('open-day', 'warning'), f('close-day', 'critical')])).toBe('close-day')
  })
  it('breaks a severity tie by count, then by the earliest checkpoint', () => {
    expect(
      defaultPhase([f('open-day', 'warning'), f('liveness', 'warning'), f('liveness', 'warning')])
    ).toBe('liveness')
    expect(defaultPhase([f('liveness', 'warning'), f('close-day', 'warning')])).toBe('liveness')
  })
  it('ranks unverified below a warning and ignores informational items', () => {
    expect(defaultPhase([f('open-day', 'unverified'), f('liveness', 'warning')])).toBe('liveness')
    expect(defaultPhase([f('open-day', 'info')])).toBeNull()
    expect(defaultPhase([])).toBeNull()
  })
})

describe('finding references', () => {
  it('carry the group and name the server resolves, for checks, steps and metrics', () => {
    const d = day({ checks: { 'open-day': result([{ name: 'trades.db', status: 'FAIL' }]) } })
    d.phases[2].steps = [{ step: 'backfill', started: '15:36:04', ended: '15:43:22', exit: 124 }]
    const refs = [
      ...buildFindings(d),
      ...metricFindings(
        [{ metric: 'Limiter refused calls (total)', today: '3575', status: 'REGRESSION' }],
        d
      ),
    ].map((f) => [f.phase, f.group, f.name])
    expect(refs).toEqual([
      ['open-day', 'open-day', 'trades.db'],
      ['close-day', 'step', 'backfill'],
      ['close-day', 'execution', 'Limiter refused calls (total)'],
    ])
  })
})
