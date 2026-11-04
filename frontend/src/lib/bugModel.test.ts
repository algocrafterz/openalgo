import { describe, expect, it } from 'vitest'
import type { Bug } from '@/api/bugs'
import {
  acceptanceCriteria,
  categoryOfBug,
  evidenceRefs,
  groupOfStatus,
  lifecycle,
  readableSources,
  reportedFromWorkflow,
  summarizeBugs,
  verificationMethods,
  wasReopened,
} from './bugModel'

function bug(over: Partial<Bug>): Bug {
  return {
    id: 'BUG-001',
    title: 'Title',
    plain: '',
    kind: 'BUG',
    severity: 'LOW',
    status: 'open',
    area: 'signal_engine',
    strategies: [],
    first_seen: '2026-11-01',
    last_seen: '2026-11-01',
    occurrences: ['2026-11-01'],
    impact: '',
    evidence: [],
    root_cause: '',
    fix: '',
    fix_commit: '',
    verification: '',
    sources: [],
    history: [],
    created_at: '',
    updated_at: '',
    ...over,
  }
}

const h = (status: Bug['status'], at: string, note = '') => ({ at, status, note })

describe('summarizeBugs', () => {
  it('counts the three groups with the high-risk split, and the done outcomes', () => {
    const s = summarizeBugs([
      bug({ status: 'open', severity: 'MEDIUM' }),
      bug({ status: 'in_progress', severity: 'HIGH' }),
      bug({ status: 'fixed', severity: 'HIGH' }),
      bug({ status: 'fixed', severity: 'HIGH' }),
      bug({ status: 'fixed', severity: 'LOW' }),
      bug({ status: 'verified' }),
      bug({ status: 'wontfix' }),
    ])
    expect(s).toEqual({
      needsFix: 2,
      highNeedsFix: 1,
      awaitingProof: 3,
      highAwaitingProof: 2,
      verified: 1,
      closedNotFixing: 1,
    })
  })
})

describe('categoryOfBug', () => {
  it('uses an explicit component first', () => {
    expect(categoryOfBug(bug({ area: 'broker', kind: 'DATA' }))).toBe('broker')
    expect(categoryOfBug(bug({ area: 'ops' }))).toBe('infra')
  })
  it('lets exactly one matching title rule decide, over the kind', () => {
    expect(
      categoryOfBug(bug({ title: 'The one-position-per-symbol cap is empty', kind: 'DATA' }))
    ).toBe('risk')
    expect(categoryOfBug(bug({ title: 'eod.sh section was always empty' }))).toBe('reporting')
    expect(categoryOfBug(bug({ title: 'Startup reconcile misses a leg' }))).toBe('execution')
  })
  it('sends a title that matches two categories to Other, not a guess', () => {
    // "stop-loss" is risk and "exit" is execution
    expect(
      categoryOfBug(bug({ title: 'Exit is booked under the stop-loss id', kind: 'BUG' }))
    ).toBe('other')
  })
  it('does not read a word like labelled as reporting', () => {
    expect(categoryOfBug(bug({ title: 'Something is labelled wrongly', kind: 'BUG' }))).toBe(
      'other'
    )
  })
  it('falls back to kind, and a bug with no signal is Other', () => {
    expect(categoryOfBug(bug({ title: 'Something odd', kind: 'DATA' }))).toBe('data')
    expect(categoryOfBug(bug({ title: 'Something odd', kind: 'EXECUTION' }))).toBe('execution')
    expect(categoryOfBug(bug({ title: 'Something odd', kind: 'TUNING-IDEA' }))).toBe('strategy')
    expect(categoryOfBug(bug({ title: 'Something odd', kind: 'BUG', area: 'signal_engine' }))).toBe(
      'other'
    )
    expect(categoryOfBug(bug({ title: 'Something odd', kind: 'BUG', area: 'openalgo-core' }))).toBe(
      'other'
    )
  })
  it('is deterministic', () => {
    const b = bug({ title: 'Exit is booked under the stop-loss id' })
    expect(categoryOfBug(b)).toBe(categoryOfBug({ ...b }))
  })
})

describe('verification helpers', () => {
  it('reads methods from the verification text, and says nothing when it names none', () => {
    expect(verificationMethods('51 value-area tests pass; store coverage 211/211')).toEqual([
      'Automated test',
    ])
    expect(
      verificationMethods('pages_vs_fills --mode analyze re-run 2026-10-31: RESULT PASS')
    ).toEqual(['Sandbox session', 'Report or check re-run'])
    expect(verificationMethods('verified by a live session on the broker fills')).toEqual([
      'Live trading session',
    ])
    expect(verificationMethods('looks fine')).toEqual([])
  })
  it('collects commit, sources and report paths quoted in the text, once each', () => {
    const b = bug({
      fix_commit: 'd2bfbffdc',
      sources: ['signal_engine/reports/eod-2026-10-26-claude-review.md'],
      verification:
        "re-run: reports/eod-2026-10-30.md:139 'INTRADAY' and signal_engine/reports/eod-2026-10-26-claude-review.md",
    })
    expect(evidenceRefs(b)).toEqual([
      'commit d2bfbffdc',
      'signal_engine/reports/eod-2026-10-26-claude-review.md',
      'reports/eod-2026-10-30.md:139',
    ])
    expect(evidenceRefs(bug({}))).toEqual([])
  })
})

describe('acceptanceCriteria', () => {
  it('reads the latest Proves it note', () => {
    const b = bug({
      history: [
        h('fixed', '2026-11-02T10:00', 'Fixed. Proves it: a timed-out exit that later fills'),
      ],
    })
    expect(acceptanceCriteria(b)).toBe('a timed-out exit that later fills')
    expect(acceptanceCriteria(bug({}))).toBeNull()
  })
})

describe('lifecycle', () => {
  const states = (b: Bug) => lifecycle(b)?.map((s) => `${s.key}:${s.state}`)
  it('open is Confirmed, current', () => {
    expect(states(bug({ history: [h('open', '2026-11-01T09:00')] }))).toEqual([
      'reported:done',
      'confirmed:current',
      'fixed:pending',
      'awaiting:pending',
      'verified:pending',
    ])
  })
  it('fixed shows Fixed as done with its recorded date and Awaiting proof as current', () => {
    const b = bug({
      status: 'fixed',
      history: [h('open', '2026-11-01T09:00'), h('fixed', '2026-11-02T10:30')],
    })
    expect(states(b)).toEqual([
      'reported:done',
      'confirmed:done',
      'fixed:done',
      'awaiting:current',
      'verified:pending',
    ])
    expect(lifecycle(b)?.[2].date).toBe('2026-11-02')
  })
  it('does not claim Fixed happened when no history entry records it', () => {
    const b = bug({ status: 'verified', history: [h('open', '2026-11-01T09:00')] })
    expect(states(b)).toEqual([
      'reported:done',
      'confirmed:done',
      'fixed:unrecorded',
      'awaiting:unrecorded',
      'verified:current',
    ])
  })
  it('dates Fixed from the start of its latest run, not from a later note', () => {
    const b = bug({
      status: 'fixed',
      history: [
        h('open', '2026-10-31T09:00'),
        h('fixed', '2026-11-01T10:00'),
        h('open', '2026-11-02T09:00'),
        h('fixed', '2026-11-03T10:00'),
        h('fixed', '2026-11-04T08:00', 'note'),
      ],
    })
    expect(lifecycle(b)?.[2].date).toBe('2026-11-03')
  })
  it('wontfix has no lifecycle, and the in-progress label is kept', () => {
    expect(lifecycle(bug({ status: 'wontfix' }))).toBeNull()
    expect(lifecycle(bug({ status: 'in_progress' }))?.[1].label).toBe('Confirmed, in progress')
  })
  it('a reopened bug goes back to Confirmed and is flagged', () => {
    const b = bug({
      status: 'open',
      history: [h('open', '2026-11-01'), h('fixed', '2026-11-02'), h('open', '2026-11-03')],
    })
    expect(wasReopened(b)).toBe(true)
    expect(lifecycle(b)?.[2].state).toBe('pending')
  })
})

describe('bugs reported from a workflow finding', () => {
  const marked = bug({
    sources: [
      'workflow-finding:open-day:open-day:close-day <date>',
      'signal_engine/reports/open-day-2026-11-03.md',
    ],
  })
  it('recognises them and hides the bookkeeping marker from readable sources and evidence', () => {
    expect(reportedFromWorkflow(marked)).toBe(true)
    expect(reportedFromWorkflow(bug({}))).toBe(false)
    expect(readableSources(marked)).toEqual(['signal_engine/reports/open-day-2026-11-03.md'])
    expect(evidenceRefs(marked).join(' ')).not.toContain('workflow-finding')
  })
  it('maps a status to the tab it lives in', () => {
    expect(groupOfStatus('in_progress')).toBe('todo')
    expect(groupOfStatus('fixed')).toBe('proving')
    expect(groupOfStatus('wontfix')).toBe('done')
  })
})
