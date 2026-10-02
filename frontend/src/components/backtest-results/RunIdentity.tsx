import type { Classification, RunScope } from '@/api/backtestResults'
import { Badge } from '@/components/ui/badge'

/** Small, secondary: which strategy version and run this page is about, and what kind it is. */
export function RunIdentity({
  ideaTitle,
  strategyVersion,
  runId,
  changeClasses,
  classification,
  scope,
  strategyCategory,
  tags,
}: {
  ideaTitle: string
  strategyVersion?: string | null
  runId: string
  changeClasses: string[]
  classification: Classification | null
  /**
   * The selected run's frozen scope. Undefined means a legacy run with no frozen scope, so the
   * idea's current classification is used; null means captured but without a scope.
   */
  scope?: RunScope | null
  strategyCategory?: string
  tags?: string[]
}) {
  const title = [ideaTitle, strategyVersion, runId].filter(Boolean).join(' · ')
  const legacy = scope === undefined
  const chips = [
    classification?.strategy_category ?? strategyCategory,
    legacy ? classification?.trading_style : scope?.style,
    legacy ? classification?.market : scope?.market,
    legacy ? classification?.timeframe : scope?.timeframe,
    legacy ? classification?.direction : scope?.direction,
  ].filter((c): c is string => Boolean(c))
  const tagList = (tags ?? classification?.strategy_tags ?? []).filter(Boolean)

  return (
    <div className="space-y-1.5 text-sm text-muted-foreground" data-testid="run-identity">
      <div className="font-medium" data-testid="run-identity-title">
        {title}
      </div>
      <div className="flex flex-wrap items-center gap-1.5" data-testid="change-classes">
        <span className="text-xs">Run type:</span>
        {changeClasses.length === 0 ? (
          <span className="text-xs">First run (no change classes recorded)</span>
        ) : (
          changeClasses.map((c, i) => (
            <Badge key={c} variant="outline" className="text-xs" data-testid="change-class">
              {c}
              {i === 0 ? ' (primary)' : ''}
            </Badge>
          ))
        )}
      </div>
      {chips.length > 0 && (
        <div className="flex flex-wrap gap-1.5" data-testid="classification">
          {chips.map((c) => (
            <Badge key={c} variant="secondary" className="text-xs">
              {c}
            </Badge>
          ))}
        </div>
      )}
      {tagList.length > 0 && (
        <div className="text-xs" data-testid="run-tags">
          Tags: {tagList.join(' · ')}
        </div>
      )}
    </div>
  )
}
