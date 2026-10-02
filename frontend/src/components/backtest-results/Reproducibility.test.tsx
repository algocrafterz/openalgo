import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { RunFingerprints, RunReproducibility } from '@/api/backtestResults'
import { Reproducibility } from './Reproducibility'

vi.mock('./InfoTip', () => ({ InfoTip: ({ term }: { term: string }) => <span>{term}</span> }))

const KEYS = [
  'strategy_version',
  'strategy_fingerprint',
  'dataset_fingerprint',
  'execution_config',
  'deployment_profile',
  'engine_version',
  'code_commit',
  'resolved_assumptions',
]

function makeRepro(over: Partial<RunReproducibility> = {}): RunReproducibility {
  return {
    reproducible: true,
    code_dirty: false,
    dirty_paths: [],
    code_commit: '9d1f3fd43aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    engine_version: '3.1.0',
    checklist: Object.fromEntries(KEYS.map((k) => [k, true])),
    last_attempt: null,
    ...over,
  }
}

const FPS: RunFingerprints = {
  rules: 'aaaaaaaa11111111',
  params: 'bbbbbbbb22222222',
  strategy: 'cccccccc33333333',
  execution: 'dddddddd44444444',
  cost: 'eeeeeeee55555555',
  universe: 'ffffffff66666666',
  profile: '11111111aaaaaaaa',
  dataset: '22222222bbbbbbbb',
}

describe('Reproducibility', () => {
  it('shows a clear yes when reproducible', () => {
    render(
      <Reproducibility reproducibility={makeRepro()} fingerprints={FPS} manifestSha256={null} />
    )
    expect(screen.getByTestId('repro-state')).toHaveTextContent('Reproducible: Yes')
    expect(screen.queryByTestId('repro-not-eligible')).not.toBeInTheDocument()
    expect(screen.getByTestId('repro-engine')).toHaveTextContent('3.1.0')
    expect(screen.getByTestId('repro-engine')).toHaveTextContent('9d1f3fd')
  })

  it('shows no with dirty paths and marks the run not eligible for promotion', () => {
    render(
      <Reproducibility
        reproducibility={makeRepro({
          reproducible: false,
          code_dirty: true,
          dirty_paths: ['signal_engine/risk.py', 'signal_engine/main.py'],
        })}
        fingerprints={FPS}
        manifestSha256={null}
      />
    )
    expect(screen.getByTestId('repro-state')).toHaveTextContent('Reproducible: No')
    expect(screen.getByTestId('repro-state')).toHaveTextContent(/uncommitted|dirty/i)
    expect(screen.getByText('signal_engine/risk.py')).toBeInTheDocument()
    expect(screen.getByText('signal_engine/main.py')).toBeInTheDocument()
    expect(screen.getByTestId('repro-not-eligible')).toHaveTextContent('Not eligible for promotion')
  })

  it('lists every checklist item and flags those not captured', () => {
    render(
      <Reproducibility
        reproducibility={makeRepro({
          reproducible: false,
          checklist: { ...Object.fromEntries(KEYS.map((k) => [k, true])), code_commit: false },
        })}
        fingerprints={FPS}
        manifestSha256={null}
      />
    )
    const items = screen.getAllByTestId(/^repro-check-/)
    expect(items).toHaveLength(8)
    expect(screen.getByTestId('repro-check-code_commit')).toHaveTextContent('Not captured')
    expect(screen.getByTestId('repro-check-code_commit')).toHaveTextContent('Code commit')
    expect(screen.getByTestId('repro-check-strategy_version')).toHaveTextContent('Captured')
    expect(screen.getByTestId('repro-state')).toHaveTextContent('Code commit')
  })

  it('shows the eight fingerprints short with the full value in a title', () => {
    render(
      <Reproducibility
        reproducibility={makeRepro()}
        fingerprints={FPS}
        manifestSha256={'9'.repeat(64)}
      />
    )
    for (const v of Object.values(FPS)) {
      expect(screen.getByText(v.slice(0, 8), { selector: '[title]' })).toHaveAttribute('title', v)
    }
    expect(screen.getByText('99999999', { selector: '[title]' })).toHaveAttribute(
      'title',
      '9'.repeat(64)
    )
  })

  it('says Never reproduced when there is no attempt', () => {
    render(
      <Reproducibility reproducibility={makeRepro()} fingerprints={FPS} manifestSha256={null} />
    )
    expect(screen.getByTestId('repro-last-attempt')).toHaveTextContent('Never reproduced')
  })

  it('shows the last attempt with when, outcome and reasons', () => {
    render(
      <Reproducibility
        reproducibility={makeRepro({
          last_attempt: {
            attempted_ist: '2026-10-02 10:15',
            reproducible: false,
            reasons: ['trade count differs'],
          },
        })}
        fingerprints={FPS}
        manifestSha256={null}
      />
    )
    const box = screen.getByTestId('repro-last-attempt')
    expect(box).toHaveTextContent('2026-10-02 10:15')
    expect(box).toHaveTextContent('Failed')
    expect(box).toHaveTextContent('trade count differs')
  })

  it('shows a passed attempt', () => {
    render(
      <Reproducibility
        reproducibility={makeRepro({
          last_attempt: { attempted_ist: '2026-10-02 10:15', reproducible: true, reasons: [] },
        })}
        fingerprints={null}
        manifestSha256={null}
      />
    )
    expect(screen.getByTestId('repro-last-attempt')).toHaveTextContent('Passed')
  })

  it('shows a not captured note when there is no reproducibility data', () => {
    render(<Reproducibility reproducibility={null} fingerprints={null} manifestSha256={null} />)
    expect(screen.getByText('Not captured for this run')).toBeInTheDocument()
  })

  it('lists change classes when given', () => {
    render(
      <Reproducibility
        reproducibility={makeRepro()}
        fingerprints={FPS}
        manifestSha256={null}
        changeClasses={['params', 'costs']}
      />
    )
    expect(screen.getByTestId('repro-change-classes')).toHaveTextContent('params')
    expect(screen.getByTestId('repro-change-classes')).toHaveTextContent('costs')
  })
})

describe('Reproducibility partial data', () => {
  it('tolerates partial fingerprints and a null checklist and dirty paths', () => {
    render(
      <Reproducibility
        reproducibility={makeRepro({
          reproducible: false,
          code_dirty: true,
          dirty_paths: null,
          checklist: null,
        } as unknown as Partial<RunReproducibility>)}
        fingerprints={{ rules: 'aaaaaaaa11111111' } as unknown as RunFingerprints}
        manifestSha256={null}
      />
    )
    expect(screen.getByTestId('repro-state')).toHaveTextContent('Reproducible: No')
    const fp = screen.getByText('Fingerprints').parentElement as HTMLElement
    expect(fp).toHaveTextContent('aaaaaaaa')
    expect(fp.textContent?.match(/not captured/g)?.length).toBe(7)
  })
})
