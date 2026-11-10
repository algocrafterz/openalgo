import { describe, expect, it } from 'vitest'
import { fmtRupees, rupeePnl } from './backtestResults'

describe('rupeePnl', () => {
  it('prefers the listing figure and passes it through unchanged', () => {
    const r = { net_rs: -16400.18, total_cost_rs: 164978.74, basis: 'modelled' }
    expect(rupeePnl({ rupee_pnl: r, cost_model: { modelled_net_pnl_rs: 1 } })).toBe(r)
  })
  it('reads the same fields from a full run record cost model', () => {
    expect(
      rupeePnl({
        cost_model: {
          modelled_net_pnl_rs: 120.5,
          modelled_total_cost_rs: 30,
          pnl_basis: 'modelled: independent sizing',
        },
      })
    ).toEqual({ net_rs: 120.5, total_cost_rs: 30, basis: 'modelled: independent sizing' })
  })
  it('is null when the run recorded no rupee result, never zero', () => {
    expect(rupeePnl({})).toBeNull()
    expect(rupeePnl({ cost_model: {} })).toBeNull()
    expect(
      rupeePnl({ rupee_pnl: null, cost_model: { modelled_net_pnl_rs: Number.NaN } })
    ).toBeNull()
  })
  it('keeps net without a cost when the cost was not recorded', () => {
    expect(rupeePnl({ cost_model: { modelled_net_pnl_rs: 5 } })).toEqual({
      net_rs: 5,
      total_cost_rs: null,
      basis: null,
    })
  })
})

describe('fmtRupees', () => {
  it('groups digits the Indian way and signs on request', () => {
    expect(fmtRupees(1234567)).toBe('Rs 12,34,567')
    expect(fmtRupees(-16400.18)).toBe('-Rs 16,400')
    expect(fmtRupees(2500, true)).toBe('+Rs 2,500')
  })
  it('never prints a signed zero and reads n/a for an unknown', () => {
    expect(fmtRupees(-0.2)).toBe('Rs 0')
    expect(fmtRupees(0, true)).toBe('Rs 0')
    expect(fmtRupees(null)).toBe('n/a')
    expect(fmtRupees(Number.NaN)).toBe('n/a')
  })
})
