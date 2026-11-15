import { describe, expect, it } from 'vitest'
import type { WorkflowDay, WorkflowRunStatus } from '@/api/opsWorkflow'
import {
  combinedHealthOf,
  noticeLines,
  noticeNeedsLook,
  phaseTime,
  recurrenceKey,
  recurrenceOf,
  recurrenceText,
  recurringFailures,
  reviewStatusText,
  runHealth,
  runLabel,
  summarizeRuns,
} from './workflowStatus'

const run = (
  phase: WorkflowRunStatus['phase'],
  state: string,
  late = false
): WorkflowRunStatus => ({
  phase,
  due: '09:12',
  state,
  late,
  started_at: null,
  ended_at: null,
  note: '',
})

const day = (extra: Partial<WorkflowDay>): WorkflowDay => ({
  day: '2026-11-13',
  phases: [],
  checks: {},
  reports: [],
  verdict: null,
  ...extra,
})

describe('runLabel', () => {
  it('names each manifest and derived state', () => {
    expect(runLabel(run('open-day', 'completed'))?.label).toBe('Completed')
    expect(runLabel(run('open-day', 'missed'))).toMatchObject({ tone: 'bad', problem: true })
    expect(runLabel(run('open-day', 'interrupted'))?.problem).toBe(true)
    expect(runLabel(run('open-day', 'legacy'))?.problem).toBe(false)
  })
  it('flags a completed run that started late', () => {
    expect(runLabel(run('liveness', 'completed', true))).toMatchObject({
      label: 'Completed, started late',
      tone: 'warn',
      problem: true,
    })
  })
  it('is null without a row', () => expect(runLabel(undefined)).toBeNull())
})

describe('summarizeRuns', () => {
  it('counts expected versus completed and lists problems', () => {
    const s = summarizeRuns([
      run('open-day', 'completed'),
      run('liveness', 'missed'),
      run('close-day', 'not_due'),
    ])
    expect(s?.text).toBe('1 of 2 expected runs completed')
    expect(s?.problems.map((p) => p.phase)).toEqual(['liveness'])
  })
  it('handles a non-trading day and absent data', () => {
    expect(summarizeRuns([run('open-day', 'not_expected')])?.text).toBe('No runs expected today.')
    expect(summarizeRuns(undefined)).toBeNull()
  })
})

describe('recurrence', () => {
  const d = day({
    recurrence: {
      window: 10,
      items: [
        {
          group: 'logs',
          name: 'Spike 2026-11-13',
          count: 3,
          first: '2026-11-03',
          last: '2026-11-13',
          failing_today: true,
        },
        {
          group: 'jobs',
          name: 'Once',
          count: 1,
          first: '2026-11-12',
          last: '2026-11-12',
          failing_today: false,
        },
      ],
    },
  })
  it('matches a name that carries a date', () => {
    expect(recurrenceKey('logs', 'Spike 2026-11-12 at 09:12')).toBe(
      recurrenceKey('logs', 'spike 2026-11-13 at 10:01')
    )
    expect(recurrenceOf(d, 'logs', 'Spike 2026-11-10')?.count).toBe(3)
    expect(recurrenceOf(d, 'open-day', 'Spike')).toBeUndefined()
  })
  it('lists only 2+ failures and words the count', () => {
    const list = recurringFailures(d)
    expect(list).toHaveLength(1)
    expect(recurrenceText(list[0], 10)).toBe(
      'Failed 3 of the last 10 sessions (first 2026-11-03, last 2026-11-13)'
    )
    expect(recurrenceText(d.recurrence!.items[1], 10)).toBe('Failed 1 of the last 10 sessions')
  })
})

describe('notifications', () => {
  it('shows a reason when unavailable', () => {
    expect(noticeLines({ available: false, reason: 'No alert has been recorded yet.' })).toEqual([
      { label: 'Alerts', value: 'No alert has been recorded yet.' },
    ])
    expect(noticeLines(undefined)).toBeNull()
  })
  it('formats times and flags a failed delivery or open red', () => {
    const n = {
      available: true,
      last_attempt_at: '2026-11-13T09:30:00+05:30',
      last_result: 'failed_after_retries',
      last_sent_at: '2026-11-13T09:13:00+05:30',
      open_red: [],
      last_recovery_at: null,
    }
    const lines = noticeLines(n)
    expect(lines?.[0].value).toBe('2026-11-13 09:30, failed after retries')
    expect(lines?.[2].value).toBe('none')
    expect(lines?.[3].value).toBe('none recorded')
    expect(noticeNeedsLook(n)).toBe(true)
    expect(noticeNeedsLook({ available: true, last_result: 'sent', open_red: [] })).toBe(false)
  })
})

describe('phaseTime', () => {
  it('reads the time the server scheduled', () => {
    const d = day({ schedule: { phases: { 'open-day': '09:20', 'close-day': '15:40' } } })
    expect(phaseTime(d, 'open-day')).toBe('09:20')
    expect(phaseTime(d, 'close-day')).toBe('15:40')
  })
  it('falls back to the built-in time when the schedule is absent, partial or malformed', () => {
    expect(phaseTime(day({}), 'open-day')).toBe('09:12')
    expect(phaseTime(undefined, 'liveness')).toBe('09:27')
    expect(phaseTime(null, 'close-day')).toBe('15:36')
    const partial = day({ schedule: { phases: { liveness: '09:30' } } })
    expect(phaseTime(partial, 'open-day')).toBe('09:12')
    const bad = day({ schedule: { phases: { 'open-day': 'soon' } } })
    expect(phaseTime(bad, 'open-day')).toBe('09:12')
  })
  it('returns an empty string for an unknown phase', () => {
    expect(phaseTime(day({}), 'nope')).toBe('')
  })
})

const rows = (a: string, b: string, c: string, late = false) => [
  run('open-day', a, late),
  run('liveness', b),
  run('close-day', c),
]
const GREEN_CHECK = {
  phase: 'open-day',
  day: '2026-11-13',
  verdict: 'GREEN' as const,
  summary: '',
  checks: [],
}
const withData = (extra: Partial<WorkflowDay>) =>
  day({
    phases: [{ phase: 'open-day', steps: [{ step: 'x', started: '', ended: '', exit: 0 }] }],
    checks: { 'open-day': GREEN_CHECK },
    ...extra,
  })

describe('runHealth and combinedHealthOf', () => {
  it('is Healthy when expected runs completed, including not due and not expected', () => {
    expect(runHealth(rows('completed', 'completed', 'not_due')).health).toBe('healthy')
    expect(runHealth(rows('not_expected', 'not_expected', 'not_expected')).health).toBe('healthy')
    expect(runHealth(rows('legacy', 'running', 'not_due')).health).toBe('healthy')
    expect(
      combinedHealthOf(withData({ run_status: rows('completed', 'completed', 'not_due') }), [])
    ).toBe('healthy')
  })
  it('a run that is not due yet does not degrade a day', () => {
    const d = withData({ run_status: rows('completed', 'not_due', 'not_due') })
    expect(combinedHealthOf(d, [])).toBe('healthy')
  })
  it('a missed run or a late start is Degraded', () => {
    expect(runHealth(rows('completed', 'missed', 'not_due')).health).toBe('degraded')
    expect(runHealth(rows('completed', 'completed', 'not_due', true)).health).toBe('degraded')
    expect(
      combinedHealthOf(withData({ run_status: rows('completed', 'missed', 'not_due') }), [])
    ).toBe('degraded')
  })
  it('a running, not due, not expected or legacy run never degrades, even if late', () => {
    for (const state of ['running', 'not_due', 'not_expected', 'legacy']) {
      expect(runHealth(rows(state, 'completed', 'completed', true)).health).toBe('healthy')
    }
  })
  it('a failed, timed out or interrupted critical run is Critical', () => {
    for (const state of ['failed', 'timed_out', 'interrupted']) {
      expect(runHealth(rows(state, 'completed', 'not_due')).health).toBe('critical')
    }
    expect(
      combinedHealthOf(withData({ run_status: rows('completed', 'completed', 'failed') }), [])
    ).toBe('critical')
  })
  it('is the worst of runs and findings', () => {
    const d = withData({ run_status: rows('completed', 'completed', 'completed') })
    const warning = { severity: 'warning' } as never
    expect(combinedHealthOf(d, [warning])).toBe('degraded')
    const worse = withData({ run_status: rows('failed', 'completed', 'not_due') })
    expect(combinedHealthOf(worse, [warning])).toBe('critical')
  })
  it('keeps the findings-only result when the server sends no run_status', () => {
    expect(combinedHealthOf(withData({}), [])).toBe('healthy')
  })
  it('a missed run on a day with no files is Degraded, not No data', () => {
    expect(combinedHealthOf(day({ run_status: rows('missed', 'not_due', 'not_due') }), [])).toBe(
      'degraded'
    )
    expect(combinedHealthOf(day({ run_status: rows('not_due', 'not_due', 'not_due') }), [])).toBe(
      'nodata'
    )
  })
})

describe('reviewStatusText', () => {
  const review = (o: Partial<NonNullable<WorkflowDay['review']>>) => ({
    outcome: 'no_verdict' as const,
    cause: 'no_block',
    text: 'The AI review produced no verdict.',
    exit: null,
    note: '',
    ...o,
  })
  it('shows the verdict when a review exists and never says there is none', () => {
    const t = reviewStatusText(
      day({
        review: review({ outcome: 'verdict', cause: '', text: 'Looks fine.' }),
        verdict: { verdict: 'GREEN', line: 'Looks fine.' },
      })
    )
    expect(t).toBe('Close-day review (GREEN): Looks fine.')
    expect(t).not.toMatch(/No close-day review/)
  })
  it('explains a review that ran without a verdict, using the server text', () => {
    expect(reviewStatusText(day({ review: review({ outcome: 'failed', cause: 'exit' }) }))).toBe(
      'Close-day review: The AI review produced no verdict.'
    )
  })
  it('absent review: wording depends on whether close-day is due', () => {
    const absent = review({ cause: 'no_report', text: 'x' })
    const at = (state: string) =>
      reviewStatusText(day({ review: absent, run_status: rows('completed', 'completed', state) }))
    expect(at('not_due')).toBe('The close-day review is not due yet. It is written after 15:36.')
    expect(at('not_expected')).toBe('No close-day review is expected on this day.')
    expect(at('running')).toMatch(/running/)
    expect(at('completed')).toBe('No close-day review recorded for this day.')
    expect(at('missed')).toBe('No close-day review recorded for this day.')
  })
  it('an older server without review still shows the verdict block', () => {
    expect(reviewStatusText(day({ verdict: { verdict: 'AMBER', line: 'Slow.' } }))).toBe(
      'Close-day review (AMBER): Slow.'
    )
  })
})
