import { describe, expect, it } from 'vitest'
import type { WorkflowDay, WorkflowRunStatus } from '@/api/opsWorkflow'
import {
  noticeLines,
  noticeNeedsLook,
  recurrenceKey,
  recurrenceOf,
  recurrenceText,
  recurringFailures,
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
