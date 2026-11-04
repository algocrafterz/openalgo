import { describe, expect, it } from 'vitest'
import {
  buildPayload,
  defaultsFor,
  findingRef,
  suggestedArea,
  suggestedKind,
} from './reportFinding'
import type { Finding } from './workflowFindings'

const finding: Finding = {
  id: 'open-day:open-day:close-day 2026-11-02',
  severity: 'critical',
  category: 'jobs',
  title: 'Yesterday end-of-day job failed',
  evidence: 'INTERNAL EVIDENCE',
  phase: 'open-day',
  group: 'open-day',
  name: 'close-day 2026-11-02',
  source: 'Readiness check',
}

describe('defaultsFor', () => {
  it('never sets a high risk and leaves the impact as Not assessed', () => {
    const d = defaultsFor(finding)
    expect(d.severity).toBe('MEDIUM')
    expect(d.impact).toBe('Not assessed')
  })
  it('suggests a kind and component from the category, ops otherwise', () => {
    expect(suggestedKind('orders')).toBe('EXECUTION')
    expect(suggestedKind('broker')).toBe('EXECUTION')
    expect(suggestedKind('data')).toBe('DATA')
    expect(suggestedKind('jobs')).toBe('BUG')
    expect(suggestedArea('broker')).toBe('broker')
    expect(suggestedArea('data')).toBe('data')
    expect(suggestedArea('infra')).toBe('ops')
  })
})

describe('buildPayload', () => {
  const edited = { ...defaultsFor(finding), impact: '  edited  ' }
  it('sends the reference, the choices and the confirmation, never the evidence or the title', () => {
    const payload = buildPayload(finding, '2026-11-03', edited, true)
    expect(payload).toEqual({
      day: '2026-11-03',
      phase: 'open-day',
      group: 'open-day',
      name: 'close-day 2026-11-02',
      severity: 'MEDIUM',
      kind: 'BUG',
      area: 'ops',
      impact: 'edited',
      severity_confirmed: true,
    })
    expect(JSON.stringify(payload)).not.toContain('INTERNAL EVIDENCE')
  })
  it('carries an unconfirmed risk level as false, so the server can refuse it', () => {
    expect(buildPayload(finding, '2026-11-03', edited, false).severity_confirmed).toBe(false)
  })
  it('falls back to Not assessed rather than an empty impact', () => {
    const blank = { ...edited, impact: '   ' }
    expect(buildPayload(finding, '2026-11-03', blank, true).impact).toBe('Not assessed')
  })
})

describe('findingRef', () => {
  it('is the same key the link list uses', () => {
    expect(findingRef(finding)).toBe('open-day|open-day|close-day 2026-11-02')
  })
})
