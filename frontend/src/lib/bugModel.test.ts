import { describe, expect, it, vi } from 'vitest'
import type { Bug } from '@/api/bugs'
import {
  ACTION_SPECS,
  acceptanceCriteria,
  ageLabel,
  availableActions,
  buildActionBody,
  buildSeverityBody,
  buildVerificationRef,
  categoryOfBug,
  evidenceRefs,
  groupOfStatus,
  isOverdue,
  lifecycle,
  nextAction,
  ownerLabel,
  readableSources,
  reportedFromWorkflow,
  returnFocus,
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

describe('server-computed fields', () => {
  it('labels age, owner and overdue from the server values only', () => {
    expect(ageLabel(bug({ age_days: 0 }))).toBe('today')
    expect(ageLabel(bug({ age_days: 1 }))).toBe('1 day old')
    expect(ageLabel(bug({ age_days: 12 }))).toBe('12 days old')
    expect(ageLabel(bug({}))).toBe('age unknown')
    expect(ageLabel(bug({ age_days: null }))).toBe('age unknown')
    expect(ownerLabel(bug({}))).toBe('Unassigned')
    expect(ownerLabel(bug({ owner: ' Priya ' }))).toBe('Priya')
    expect(isOverdue(bug({ overdue: true }))).toBe(true)
    expect(isOverdue(bug({}))).toBe(false)
  })

  it('prefers the server next action and falls back by status', () => {
    expect(nextAction(bug({ next_action: 'Do it' }))).toBe('Do it')
    expect(nextAction(bug({ status: 'open' }))).toBe('Assign and start')
    expect(nextAction(bug({ status: 'in_progress' }))).toBe('Finish fix')
    expect(nextAction(bug({ status: 'fixed' }))).toBe('Verify with evidence')
    expect(nextAction(bug({ status: 'verified' }))).toBe('None')
  })
})

describe('actions', () => {
  it('offers only actions the transition table can accept', () => {
    expect(availableActions('open')).not.toContain('verify')
    expect(availableActions('open')).not.toContain('fix')
    expect(availableActions('in_progress')).toContain('fix')
    expect(availableActions('fixed')).toContain('verify')
    expect(availableActions('verified')).toEqual(['note', 'reopen'])
    expect(availableActions('verified')).not.toContain('verify')
    expect(availableActions('wontfix')).not.toContain('verify')
  })

  it('requires the text each action needs and sends nothing else', () => {
    for (const [action, field] of [
      ['assign', 'owner'],
      ['note', 'text'],
      ['fix', 'fix'],
      ['verify', 'evidence'],
      ['reopen', 'reason'],
      ['wontfix', 'reason'],
    ] as const) {
      expect(buildActionBody(action, {})).toMatchObject({ ok: false })
      expect(buildActionBody(action, { [field]: '   ' })).toMatchObject({ ok: false })
      expect(buildActionBody(action, { [field]: ' x ' })).toEqual({
        ok: true,
        body: { [field]: 'x' },
      })
    }
    expect(buildActionBody('start', {})).toEqual({ ok: true, body: {} })
    expect(
      buildActionBody('fix', { fix: 'f', commit: '', fix_explanation: ' why ', evidence: 'no' })
    ).toEqual({ ok: true, body: { fix: 'f', fix_explanation: 'why' } })
  })

  it('has a spec for every action and no status field anywhere', () => {
    expect(Object.keys(ACTION_SPECS).sort()).toEqual([
      'assign',
      'fix',
      'note',
      'reopen',
      'severity',
      'start',
      'verify',
      'wontfix',
    ])
    for (const spec of Object.values(ACTION_SPECS))
      for (const f of spec.fields) expect(f.key).not.toBe('status')
  })

  it('offers a risk change at every status, outside the status-based buttons', () => {
    for (const s of ['open', 'in_progress', 'fixed', 'verified', 'wontfix'] as const)
      expect(availableActions(s)).not.toContain('severity')
  })

  it('builds a risk change only for a different level and a reason', () => {
    expect(buildSeverityBody('MEDIUM', 'MEDIUM', 'x')).toMatchObject({ ok: false })
    expect(buildSeverityBody('MEDIUM', 'HIGH', '  ')).toMatchObject({ ok: false })
    expect(buildSeverityBody('MEDIUM', 'HIGH', ' stop risk ')).toEqual({
      ok: true,
      body: { severity: 'HIGH', reason: 'stop risk' },
    })
  })

  it('builds an optional verification reference', () => {
    expect(buildVerificationRef('test', '   ')).toEqual({ ok: true })
    expect(buildVerificationRef('commit', ' abc1234 ')).toEqual({
      ok: true,
      ref: { kind: 'commit', ref: 'abc1234' },
    })
    expect(buildVerificationRef('test', 'x'.repeat(301))).toMatchObject({ ok: false })
    expect(buildVerificationRef('test', 'x'.repeat(300))).toMatchObject({ ok: true })
  })
})

describe('returnFocus', () => {
  it('focuses the opener while it is still on the page', () => {
    const focus = vi.fn()
    expect(returnFocus({ isConnected: true, focus })).toBe(true)
    expect(focus).toHaveBeenCalledOnce()
  })
  it('does nothing when the opener is gone or missing', () => {
    const focus = vi.fn()
    expect(returnFocus({ isConnected: false, focus })).toBe(false)
    expect(returnFocus(null)).toBe(false)
    expect(focus).not.toHaveBeenCalled()
  })
})
