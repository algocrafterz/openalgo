import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { RunScope } from '@/api/backtestResults'
import { RunIdentity } from './RunIdentity'

const CLS = {
  strategy_category: 'Mean reversion',
  strategy_tags: [],
  trading_style: 'Intraday',
  direction: 'Long only',
  timeframe: '5m',
  market: 'NSE equity',
}

describe('RunIdentity', () => {
  it('shows title, version and run id', () => {
    render(
      <RunIdentity
        ideaTitle="VWAP pullback"
        strategyVersion="v3"
        runId="run-1"
        changeClasses={['rules']}
        classification={null}
      />
    )
    expect(screen.getByTestId('run-identity-title')).toHaveTextContent('VWAP pullback · v3 · run-1')
  })

  it('marks the first change class as primary', () => {
    render(
      <RunIdentity
        ideaTitle="T"
        strategyVersion="v1"
        runId="r"
        changeClasses={['rules', 'costs']}
        classification={null}
      />
    )
    const items = screen.getAllByTestId('change-class')
    expect(items).toHaveLength(2)
    expect(items[0]).toHaveTextContent('rules')
    expect(items[0]).toHaveTextContent('primary')
    expect(items[1]).not.toHaveTextContent('primary')
  })

  it('reads First run for an empty change class list', () => {
    render(
      <RunIdentity
        ideaTitle="T"
        strategyVersion="v1"
        runId="r"
        changeClasses={[]}
        classification={null}
      />
    )
    expect(screen.getByTestId('change-classes')).toHaveTextContent(
      /First run|No change classes recorded/
    )
  })

  it('renders classification chips and tags', () => {
    render(
      <RunIdentity
        ideaTitle="T"
        strategyVersion="v1"
        runId="r"
        changeClasses={[]}
        classification={CLS}
        tags={['VWAP', 'Pullback']}
      />
    )
    const chips = screen.getByTestId('classification')
    for (const t of ['Mean reversion', 'Intraday', 'NSE equity', '5m', 'Long only']) {
      expect(chips).toHaveTextContent(t)
    }
    expect(screen.getByTestId('run-tags')).toHaveTextContent('Tags: VWAP · Pullback')
  })

  it('omits missing pieces rather than printing null', () => {
    const { container } = render(
      <RunIdentity
        ideaTitle="T"
        strategyVersion={null as unknown as string}
        runId="r"
        changeClasses={[]}
        classification={{
          ...CLS,
          trading_style: null,
          direction: null,
          timeframe: null,
          market: null,
        }}
      />
    )
    expect(container).not.toHaveTextContent(/null|undefined/)
    expect(screen.getByTestId('classification')).toHaveTextContent('Mean reversion')
    expect(screen.queryByTestId('run-tags')).not.toBeInTheDocument()
    expect(screen.getByTestId('run-identity-title')).toHaveTextContent('T · r')
  })

  it('falls back to strategyCategory when classification is missing', () => {
    render(
      <RunIdentity
        ideaTitle="T"
        strategyVersion="v1"
        runId="r"
        changeClasses={[]}
        classification={null}
        strategyCategory="Momentum"
      />
    )
    expect(screen.getByTestId('classification')).toHaveTextContent('Momentum')
  })
})

describe('RunIdentity frozen scope', () => {
  const scope = {
    market: 'NFO options',
    style: 'swing',
    direction: 'short_only',
    timeframe: '1d',
  } as unknown as RunScope

  it('takes style, direction, timeframe and market from the frozen scope', () => {
    render(
      <RunIdentity
        ideaTitle="T"
        runId="r"
        changeClasses={[]}
        classification={CLS}
        scope={scope}
        strategyCategory="Own category"
        tags={['alpha']}
      />
    )
    const chips = screen.getByTestId('classification')
    for (const v of ['NFO options', 'swing', 'short_only', '1d', 'Mean reversion']) {
      expect(chips).toHaveTextContent(v)
    }
    for (const v of ['Intraday', 'Long only', '5m', 'NSE equity']) {
      expect(chips).not.toHaveTextContent(v)
    }
    expect(screen.getByTestId('run-tags')).toHaveTextContent('alpha')
  })

  it('shows no style chips from the idea when the captured scope is missing', () => {
    render(
      <RunIdentity
        ideaTitle="T"
        runId="r"
        changeClasses={[]}
        classification={CLS}
        scope={null}
        strategyCategory="Own category"
      />
    )
    const chips = screen.getByTestId('classification')
    expect(chips).toHaveTextContent('Mean reversion')
    expect(chips).not.toHaveTextContent('Intraday')
    expect(chips).not.toHaveTextContent('5m')
  })

  it('falls back to the idea classification for a legacy run', () => {
    render(<RunIdentity ideaTitle="T" runId="r" changeClasses={[]} classification={CLS} />)
    expect(screen.getByTestId('classification')).toHaveTextContent('Intraday')
  })
})
