import type { ReactNode } from 'react'
import type { RunDetail, RunRecord } from '@/api/backtestResults'
import { type BriefTone, buildDecisionBrief } from '@/lib/decisionBrief'
import type { SummaryIdea } from '@/lib/runSummary'
import { cn } from '@/lib/utils'
import { PortfolioChart } from './RunSummary'

const TONE_CLASS: Record<BriefTone, string> = {
  stop: 'border-red-500/60 bg-red-500/5',
  wait: 'border-amber-500/60 bg-amber-500/5',
  go: 'border-green-600/60 bg-green-600/5',
  unknown: '',
}

function Question({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="space-y-2">
      <h2 className="text-sm font-semibold text-muted-foreground">{title}</h2>
      {children}
    </div>
  )
}

/**
 * The first screen of a run: four plain answers a trader needs, and nothing else. Every figure
 * names its basis; every finding links to its evidence in the collapsed sections below.
 */
export function DecisionBrief({
  idea,
  run,
  detail,
}: {
  idea: SummaryIdea
  run: RunRecord
  detail: RunDetail
}) {
  const b = buildDecisionBrief(idea, run, detail)
  return (
    <section className="max-w-5xl space-y-5" data-testid="decision-brief" aria-label="Decision">
      <Question title="Should I pursue this strategy?">
        <div
          className={cn('rounded-md border p-3', TONE_CLASS[b.tone])}
          data-testid="brief-decision"
        >
          <div className="text-lg font-semibold">{b.headline}</div>
          <div className="text-sm">{b.reason}</div>
        </div>
      </Question>

      <Question title="How did it perform?">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4" data-testid="brief-figures">
          {b.figures.map((f) => (
            <div key={f.label} className="rounded-md border p-2">
              <div className="text-xs text-muted-foreground">{f.label}</div>
              <div
                className={cn(
                  'font-mono font-semibold',
                  f.value.length > 12 ? 'text-sm' : 'text-lg'
                )}
              >
                {f.value}
              </div>
              {f.hint && <div className="text-xs text-muted-foreground">{f.hint}</div>}
            </div>
          ))}
        </div>
        <p className="text-xs text-muted-foreground" data-testid="brief-basis">
          {b.basis}
        </p>
        <PortfolioChart portfolio={b.portfolio} />
      </Question>

      <Question title="Why?">
        {b.findings.length > 0 ? (
          <ul className="list-disc space-y-1 pl-5 text-sm" data-testid="brief-findings">
            {b.findings.map((f) => (
              <li key={f.href}>
                {f.text}{' '}
                <a className="text-xs underline decoration-dotted" href={f.href}>
                  details
                </a>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm" data-testid="brief-findings">
            No test failed.
          </p>
        )}
        {b.moreFailed > 0 && (
          <p className="text-xs text-muted-foreground" data-testid="brief-more-failed">
            {b.moreFailed} more failed test{b.moreFailed === 1 ? ' is' : 's are'} listed under "How
            this decision was reached".
          </p>
        )}
        {b.caveats.length > 0 && (
          <ul
            className="list-disc space-y-0.5 pl-5 text-sm text-amber-700 dark:text-amber-400"
            data-testid="brief-caveats"
          >
            {b.caveats.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
        )}
        {b.supporting && (
          <p className="text-xs text-muted-foreground" data-testid="brief-supporting">
            {b.supporting}
          </p>
        )}
      </Question>

      <Question title="What should I do next?">
        <div className="rounded-md border p-3" data-testid="brief-action">
          <div className="font-medium">{b.action.text}</div>
          {b.action.why && <div className="text-sm text-muted-foreground">{b.action.why}</div>}
        </div>
      </Question>
    </section>
  )
}
