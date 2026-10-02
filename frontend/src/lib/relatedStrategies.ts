import type { IdeaCard } from '@/api/backtestResults'
import { ideaStrategyCategory, ideaTags, OTHER_CATEGORY } from '@/lib/backtestResults'

export interface SharedTagMatch {
  idea: IdeaCard
  tags: string[]
}

export interface RelatedStrategies {
  sameCategory: IdeaCard[]
  sharedTags: SharedTagMatch[]
}

function hasCategory(idea: IdeaCard): boolean {
  return ideaStrategyCategory(idea) !== OTHER_CATEGORY
}

/** Siblings by strategy category and by shared tags. Never includes the current idea. */
export function relatedStrategies(current: IdeaCard, all: IdeaCard[]): RelatedStrategies {
  const others = all.filter((i) => i.id !== current.id)
  const category = ideaStrategyCategory(current)
  const sameCategory = hasCategory(current)
    ? others
        .filter((i) => ideaStrategyCategory(i) === category)
        .sort((a, b) => a.title.localeCompare(b.title))
    : []

  const mine = ideaTags(current)
  const mineKeys = new Set(mine.map((t) => t.toLowerCase()))
  const sharedTags: SharedTagMatch[] = []
  if (mineKeys.size > 0) {
    for (const idea of others) {
      const tags = ideaTags(idea).filter((t) => mineKeys.has(t.toLowerCase()))
      if (tags.length > 0) sharedTags.push({ idea, tags })
    }
    sharedTags.sort(
      (a, b) => b.tags.length - a.tags.length || a.idea.title.localeCompare(b.idea.title)
    )
  }
  return { sameCategory, sharedTags }
}
