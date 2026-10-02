import { Link } from 'react-router'
import type { IdeaCard } from '@/api/backtestResults'
import { ideaVerdict } from '@/lib/backtestResults'
import { relatedStrategies } from '@/lib/relatedStrategies'
import { VerdictBadge } from './VerdictBadge'

interface RelatedStrategiesProps {
  current: IdeaCard
  ideas: IdeaCard[]
}

function classificationLine(idea: IdeaCard): string {
  const c = idea.classification
  if (!c) return ''
  return [c.strategy_category, c.trading_style, c.market, c.timeframe]
    .filter((p): p is string => Boolean(p))
    .join(' · ')
}

function Row({ idea }: { idea: IdeaCard }) {
  const line = classificationLine(idea)
  return (
    <li className="flex items-center justify-between gap-3 py-1">
      <div className="min-w-0">
        <Link to={`/backtest-results/${idea.id}`} className="text-sm font-medium hover:underline">
          {idea.title}
        </Link>
        {line && <div className="text-xs text-muted-foreground">{line}</div>}
      </div>
      <VerdictBadge verdict={ideaVerdict(idea)} />
    </li>
  )
}

/** Sibling strategies found through category and shared tags. Nothing is recomputed here. */
export function RelatedStrategies({ current, ideas }: RelatedStrategiesProps) {
  const { sameCategory, sharedTags } = relatedStrategies(current, ideas)
  if (sameCategory.length === 0 && sharedTags.length === 0) return null
  const category = current.strategy_category || current.classification?.strategy_category
  return (
    <section className="space-y-3" aria-label="Related strategies">
      <h2 className="text-sm font-semibold">Related strategies</h2>
      {sameCategory.length > 0 && (
        <div data-testid="related-category">
          <div className="text-sm font-medium">{category}</div>
          <ul className="border-l pl-3">
            {sameCategory.map((i) => (
              <Row key={i.id} idea={i} />
            ))}
          </ul>
        </div>
      )}
      {sharedTags.length > 0 && (
        <div data-testid="related-tags" className="space-y-2">
          {sharedTags.map(({ idea, tags }) => (
            <div key={idea.id}>
              <div className="text-xs text-muted-foreground">{`Shares tags: ${tags.join(', ')}`}</div>
              <ul className="border-l pl-3">
                <Row idea={idea} />
              </ul>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}
