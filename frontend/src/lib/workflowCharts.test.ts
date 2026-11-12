import { describe, expect, it } from 'vitest'
import { dayBars, formatDuration, stepBars, stepSeconds } from './workflowCharts'

describe('step timing', () => {
  it('measures a step from its start and end', () => {
    expect(stepSeconds({ started: '15:36:03', ended: '17:56:08' })).toBe(8405)
    expect(stepSeconds({ started: '09:12:03', ended: '09:12:27' })).toBe(24)
  })

  it('never goes negative when the clock moved', () => {
    expect(stepSeconds({ started: '10:00:00', ended: '09:00:00' })).toBe(0)
  })

  it('formats seconds, minutes and hours', () => {
    expect(formatDuration(24)).toBe('24s')
    expect(formatDuration(125)).toBe('2m 5s')
    expect(formatDuration(8405)).toBe('2h 20m')
  })

  it('scales bars to the longest step with a visible minimum', () => {
    const bars = stepBars([
      { step: 'eod', started: '15:36:00', ended: '15:36:13' },
      { step: 'backfill', started: '15:36:00', ended: '17:56:00' },
      { step: 'logs', started: '15:36:17', ended: '15:36:17' },
    ])
    expect(bars.map((b) => b.pct)).toEqual([3, 100, 3])
    expect(bars[1].seconds).toBe(8400)
  })

  it('handles no steps and all-zero steps', () => {
    expect(stepBars([])).toEqual([])
    expect(stepBars([{ step: 'a', started: '09:00:00', ended: '09:00:00' }])[0].pct).toBe(3)
  })
})

describe('day bars', () => {
  it('reads oldest first and scales to the worst day', () => {
    const bars = dayBars([
      { day: '2026-11-11', critical: 5, warning: 15, hasData: true },
      { day: '2026-11-05', critical: 0, warning: 5, hasData: true },
    ])
    expect(bars.map((b) => b.day)).toEqual(['2026-11-05', '2026-11-11'])
    expect(bars[1].criticalPct).toBe(25)
    expect(bars[1].warningPct).toBe(75)
    expect(dayBars([{ day: 'a', critical: 1, warning: 40, hasData: true }])[0].criticalPct).toBe(12)
    expect(bars[0].warningPct).toBe(25)
  })

  it('does not divide by zero on an all-clear strip', () => {
    const [bar] = dayBars([{ day: '2026-11-11', critical: 0, warning: 0, hasData: true }])
    expect(bar.criticalPct).toBe(0)
    expect(bar.warningPct).toBe(0)
  })
})
