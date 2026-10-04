import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type {
  CostComponentSummary,
  DataQualityReport,
  LedgerSummary,
  ValidityBlock,
} from '@/api/backtestResults'
import { TooltipProvider } from '@/components/ui/tooltip'
import { CostDecomposition } from './CostDecomposition'
import { DataQualityBlock } from './DataQualityBlock'
import { IntegrityChecks } from './IntegrityChecks'
import { LedgerSummaryBlock } from './LedgerSummaryBlock'
import { TrialsBlock } from './TrialsBlock'

const wrap = (ui: React.ReactElement) => render(<TooltipProvider>{ui}</TooltipProvider>)

const DQ: DataQualityReport = {
  schema: 1,
  status: 'WARNING',
  affects_validity: true,
  thresholds: { min_complete_pct: 99 },
  items: [
    {
      check: 'missing_sessions',
      severity: 'warning',
      detail: 'Sessions missing for 3 symbols',
      count: 12,
      symbols: ['SBIN', 'INFY'],
    },
    { check: 'late_start', severity: 'info', detail: 'Listed late', count: 1, symbols: [] },
  ],
  summary: { symbols: 208, audited_symbols: ['SBIN', 'INFY', 'TCS'] },
  dropped: {
    clean_dropped: {
      SBIN: { duplicate_ts: 2, outside_hours: 3, impossible: 1 },
      INFY: { duplicate_ts: 1, outside_hours: 0, impossible: 0 },
    },
    split_days_dropped: { SBIN: ['2024-01-02', '2024-01-03'], TCS: ['2023-05-04'] },
    symbols_dropped_min_sessions: { XYZ: 10 },
  },
  policy: {
    corporate_actions: 'Prices are as supplied; splits are not adjusted.',
    closing_auction: 'The closing auction is not modelled.',
    timestamps: 'All timestamps are IST.',
  },
}

describe('DataQualityBlock', () => {
  it('shows status, validity sentence, items, dropped counts, sample note and policy', () => {
    wrap(<DataQualityBlock dataQuality={DQ} />)
    expect(screen.getByTestId('dq-status')).toHaveTextContent('WARNING')
    expect(screen.getByTestId('dq-validity')).toHaveTextContent(
      'This warning makes the run invalid, so the verdict is INCONCLUSIVE'
    )
    const rows = within(screen.getByTestId('dq-items')).getAllByRole('row')
    expect(rows).toHaveLength(3)
    expect(rows[1]).toHaveTextContent('missing_sessions')
    expect(rows[1]).toHaveTextContent('warning')
    expect(rows[1]).toHaveTextContent('Sessions missing for 3 symbols')
    expect(rows[1]).toHaveTextContent('12')
    expect(rows[1]).toHaveTextContent('SBIN, INFY')
    const dropped = screen.getByTestId('dq-dropped')
    expect(dropped).toHaveTextContent('Duplicate timestamps')
    expect(dropped).toHaveTextContent('Rows outside trading hours')
    expect(dropped).toHaveTextContent('Impossible rows')
    expect(within(dropped).getByTestId('dropped-duplicate_ts')).toHaveTextContent('3')
    expect(within(dropped).getByTestId('dropped-outside_hours')).toHaveTextContent('3')
    expect(within(dropped).getByTestId('dropped-split_days')).toHaveTextContent('3')
    expect(within(dropped).getByTestId('dropped-symbols')).toHaveTextContent('1')
    expect(screen.getByTestId('dq-sample')).toHaveTextContent(
      'Audited a sample of 3 of 208 symbols'
    )
    const policy = screen.getByRole('list', {
      name: 'What the data layer does and does not handle',
    })
    expect(policy).toHaveTextContent('splits are not adjusted')
    expect(policy).toHaveTextContent('closing auction is not modelled')
    expect(policy).toHaveTextContent('All timestamps are IST')
  })

  it('omits the validity sentence and sample note for a clean full-coverage PASS', () => {
    wrap(
      <DataQualityBlock
        dataQuality={{
          ...DQ,
          status: 'PASS',
          affects_validity: false,
          items: [],
          summary: { symbols: 2, audited_symbols: ['A', 'B'] },
          dropped: {},
        }}
      />
    )
    expect(screen.getByTestId('dq-status')).toHaveTextContent('PASS')
    expect(screen.queryByTestId('dq-validity')).not.toBeInTheDocument()
    expect(screen.queryByTestId('dq-sample')).not.toBeInTheDocument()
    expect(screen.getByText('No data-quality items were reported.')).toBeInTheDocument()
  })

  it('renders NOT_RUN without claiming a pass', () => {
    wrap(
      <DataQualityBlock
        dataQuality={{ ...DQ, status: 'NOT_RUN', affects_validity: false, items: [] }}
      />
    )
    expect(screen.getByTestId('dq-status')).toHaveTextContent('NOT_RUN')
    expect(screen.getByTestId('dq-status').className).not.toMatch(/green/)
  })

  it('says not captured for null', () => {
    wrap(<DataQualityBlock dataQuality={null} />)
    expect(screen.getByText('Not captured for this run')).toBeInTheDocument()
  })

  it('tolerates missing summary and dropped data', () => {
    wrap(
      <DataQualityBlock
        dataQuality={{ ...DQ, summary: {}, dropped: {}, items: [], affects_validity: false }}
      />
    )
    expect(screen.getByTestId('dq-status')).toBeInTheDocument()
  })
})

describe('IntegrityChecks', () => {
  const full: ValidityBlock = {
    ok: true,
    failures: [],
    checks_run: ['data_quality', 'config', 'lineage', 'truncation'],
  }

  it('shows each of the four checks as passed', () => {
    wrap(<IntegrityChecks validity={full} />)
    for (const c of ['data_quality', 'config', 'lineage', 'truncation']) {
      expect(screen.getByTestId(`check-${c}`)).toHaveTextContent('Passed')
    }
  })

  it('shows a failure with its detail', () => {
    wrap(
      <IntegrityChecks
        validity={{
          ok: false,
          failures: [{ check: 'lineage', detail: 'orders do not reconcile' }],
          checks_run: ['data_quality', 'config', 'lineage', 'truncation'],
        }}
      />
    )
    expect(screen.getByTestId('check-lineage')).toHaveTextContent('Failed')
    expect(screen.getByTestId('check-lineage')).toHaveTextContent('orders do not reconcile')
    expect(screen.getByTestId('check-config')).toHaveTextContent('Passed')
  })

  it('never styles a check that did not run like a pass', () => {
    wrap(<IntegrityChecks validity={{ ok: true, failures: [], checks_run: ['config'] }} />)
    const el = screen.getByTestId('check-truncation')
    expect(el).toHaveTextContent('Did not run')
    expect(el).not.toHaveTextContent('Passed')
    expect(el.querySelector('[data-state]')?.className ?? '').not.toMatch(/green/)
    expect(el.innerHTML).not.toMatch(/green/)
    expect(screen.getByTestId('check-config')).toHaveTextContent('Passed')
  })

  it('notes the look-ahead check scope', () => {
    wrap(<IntegrityChecks validity={full} />)
    expect(screen.getByTestId('lookahead-note')).toHaveTextContent(
      'single-symbol, frame-derived inputs only'
    )
    expect(screen.getByTestId('lookahead-note')).toHaveTextContent('regime or cross-sectional')
  })

  it('says not recorded for null', () => {
    wrap(<IntegrityChecks validity={null} />)
    expect(screen.getByText('Validity checks were not recorded for this run')).toBeInTheDocument()
  })
})

describe('TrialsBlock', () => {
  it('shows material, external, total and source', () => {
    wrap(<TrialsBlock trials={{ material: 3, external: 2, total: 5, source: 'trials.yaml' }} />)
    expect(screen.getByTestId('trials-material')).toHaveTextContent('3')
    expect(screen.getByTestId('trials-external')).toHaveTextContent('2')
    expect(screen.getByTestId('trials-total')).toHaveTextContent('5')
    expect(screen.getByText(/trials\.yaml/)).toBeInTheDocument()
    expect(screen.getByTestId('trials-explain')).toBeInTheDocument()
  })

  it('says not recorded for null', () => {
    wrap(<TrialsBlock trials={null} />)
    expect(screen.getByText('Not recorded')).toBeInTheDocument()
  })
})

describe('LedgerSummaryBlock', () => {
  const ledgers: LedgerSummary = {
    signals: 100,
    accepted: 60,
    excluded_by_reason: { OPEN_WINDOW: 10, MAX_TRADES_DAY: 25, MYSTERY: 5 },
    orders: 60,
    filled: 55,
    dropped: 5,
    fills: 110,
    trades: 55,
    reconcile_problems: 0,
    problems: [],
  }

  it('shows counts, exact reconciliation and exclusions sorted by count', () => {
    wrap(<LedgerSummaryBlock ledgers={ledgers} />)
    for (const [k, v] of Object.entries({
      signals: '100',
      accepted: '60',
      orders: '60',
      filled: '55',
      dropped: '5',
      fills: '110',
      trades: '55',
    })) {
      expect(screen.getByTestId(`ledger-${k}`)).toHaveTextContent(v)
    }
    expect(screen.getByTestId('ledger-reconcile')).toHaveTextContent('Reconciles exactly')
    const rows = within(screen.getByTestId('ledger-exclusions')).getAllByRole('row').slice(1)
    expect(rows.map((r) => r.textContent)).toEqual([
      'MAX_TRADES_DAYDaily trade limit reached25',
      'OPEN_WINDOWInside the opening window10',
      'MYSTERYMYSTERY5',
    ])
  })

  it('lists reconciliation problems', () => {
    wrap(
      <LedgerSummaryBlock
        ledgers={{ ...ledgers, reconcile_problems: 2, problems: ['orders 61 != 60', 'fills odd'] }}
      />
    )
    const el = screen.getByTestId('ledger-reconcile')
    expect(el).not.toHaveTextContent('Reconciles exactly')
    expect(el).toHaveTextContent('orders 61 != 60')
    expect(el).toHaveTextContent('fills odd')
  })

  it('handles no exclusions', () => {
    wrap(<LedgerSummaryBlock ledgers={{ ...ledgers, excluded_by_reason: {} }} />)
    expect(screen.getByText('No signals were excluded.')).toBeInTheDocument()
  })

  it('says no ledgers for null', () => {
    wrap(<LedgerSummaryBlock ledgers={null} />)
    expect(screen.getByText('No lineage ledgers for this run')).toBeInTheDocument()
  })
})

describe('CostDecomposition', () => {
  const cc: CostComponentSummary = {
    trades: 100,
    components: [
      { key: 'brokerage', label: 'Brokerage', mean_bps: 1 },
      { key: 'stt', label: 'STT', mean_bps: 3 },
    ],
    modeled_cost_bps_mean: 4,
    engine_cost_bps_mean: 9.4,
  }

  it('shows before-cost and after-cost figures side by side', () => {
    wrap(<CostDecomposition costComponents={cc} grossBps={8.1} netBps={-1.3} />)
    expect(screen.getByTestId('cost-before')).toHaveTextContent('8.10')
    expect(screen.getByTestId('cost-after')).toHaveTextContent('-1.30')
  })

  it('lists components with mean bps and share of total, plus both cost numbers and the note', () => {
    wrap(<CostDecomposition costComponents={cc} grossBps={8.1} netBps={-1.3} />)
    const rows = within(screen.getByTestId('cost-components')).getAllByRole('row').slice(1)
    expect(rows[0]).toHaveTextContent('Brokerage')
    expect(rows[0]).toHaveTextContent('1.00')
    expect(rows[0]).toHaveTextContent('25.0%')
    expect(rows[1]).toHaveTextContent('75.0%')
    expect(screen.getByTestId('cost-engine')).toHaveTextContent('9.40')
    expect(screen.getByTestId('cost-modeled')).toHaveTextContent('4.00')
    expect(screen.getByTestId('cost-note')).toHaveTextContent(
      "The headline net result uses the engine's flat cost; these modelled components show where a realistic cost comes from"
    )
  })

  it('handles a null engine cost and zero total without NaN', () => {
    wrap(
      <CostDecomposition
        costComponents={{
          trades: 1,
          components: [{ key: 'a', label: 'A', mean_bps: 0 }],
          modeled_cost_bps_mean: 0,
          engine_cost_bps_mean: null,
        }}
        grossBps={1}
        netBps={1}
      />
    )
    expect(screen.getByTestId('cost-engine')).toHaveTextContent('n/a')
    expect(document.body.textContent).not.toContain('NaN')
  })

  it('says not recorded for null but still shows the headline figures', () => {
    wrap(<CostDecomposition costComponents={null} grossBps={8.1} netBps={-1.3} />)
    expect(screen.getByText('Cost components were not recorded')).toBeInTheDocument()
    expect(screen.getByTestId('cost-before')).toBeInTheDocument()
  })
})

describe('defensive rendering', () => {
  it('data quality tolerates missing items', () => {
    wrap(
      <DataQualityBlock dataQuality={{ ...DQ, items: undefined } as unknown as DataQualityReport} />
    )
    expect(screen.getByText('No data-quality items were reported.')).toBeInTheDocument()
  })

  it('cost block tolerates missing components', () => {
    wrap(
      <CostDecomposition
        costComponents={
          { trades: 1, modeled_cost_bps_mean: 0, engine_cost_bps_mean: 1 } as CostComponentSummary
        }
        grossBps={1}
        netBps={1}
      />
    )
    expect(screen.getByTestId('cost-engine')).toHaveTextContent('1.00')
  })

  it('integrity checks show unknown failures and an overall Not ok line', () => {
    wrap(
      <IntegrityChecks
        validity={{
          ok: false,
          failures: [{ check: 'mystery_check', detail: 'odd thing' }],
          checks_run: ['config'],
        }}
      />
    )
    expect(screen.getByTestId('check-mystery_check')).toHaveTextContent('odd thing')
    expect(screen.getByTestId('validity-overall')).toHaveTextContent('Not ok')
  })

  it('integrity checks show Not ok even when no failure row exists', () => {
    wrap(<IntegrityChecks validity={{ ok: false, failures: [], checks_run: ['config'] }} />)
    expect(screen.getByTestId('validity-overall')).toHaveTextContent('Not ok')
  })

  it('integrity checks tolerate missing arrays', () => {
    wrap(<IntegrityChecks validity={{ ok: true } as unknown as ValidityBlock} />)
    expect(screen.getByTestId('check-config')).toHaveTextContent('Did not run')
    expect(screen.queryByTestId('validity-overall')).not.toBeInTheDocument()
  })

  it('ledger block never claims exact reconciliation for a non-number problem count', () => {
    wrap(
      <LedgerSummaryBlock
        ledgers={
          {
            signals: 1,
            accepted: 1,
            excluded_by_reason: {},
            orders: 1,
            filled: 1,
            dropped: 0,
            fills: 2,
            trades: 1,
            problems: [],
          } as unknown as LedgerSummary
        }
      />
    )
    const el = screen.getByTestId('ledger-reconcile')
    expect(el).toHaveTextContent('Reconciliation not recorded')
    expect(el).not.toHaveTextContent('Reconciles exactly')
    expect(el).not.toHaveTextContent('undefined')
  })

  it('ledger counts use the number formatter', () => {
    wrap(
      <LedgerSummaryBlock
        ledgers={
          {
            signals: null,
            accepted: 1,
            excluded_by_reason: {},
            orders: 1,
            filled: 1,
            dropped: 0,
            fills: 2,
            trades: 1,
            reconcile_problems: 0,
            problems: [],
          } as unknown as LedgerSummary
        }
      />
    )
    expect(screen.getByTestId('ledger-signals')).toHaveTextContent('n/a')
  })
})
