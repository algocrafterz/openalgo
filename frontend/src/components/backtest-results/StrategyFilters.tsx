import { useMemo } from 'react'
import type { IdeaCard } from '@/api/backtestResults'
import { Input } from '@/components/ui/input'
import {
  ALL,
  EMPTY_FILTERS,
  type FacetKey,
  type FacetOption,
  facetOptionsFor,
  type IdeaFilters,
  legacyOptionsFor,
  tagOptionsFor,
} from '@/lib/backtestResults'
import { cn } from '@/lib/utils'

function FilterSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: string
  options: FacetOption[]
  onChange: (v: string) => void
}) {
  return (
    <label className="text-sm flex flex-col gap-1">
      {label}
      <select
        className="border-input bg-background rounded-md border px-2 py-1.5 text-sm"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value={ALL}>All</option>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {`${o.value} (${o.count})`}
          </option>
        ))}
      </select>
    </label>
  )
}

const LEGACY: { label: string; key: 'status' | 'category' | 'horizon' }[] = [
  { label: 'Status', key: 'status' },
  { label: 'Library group', key: 'category' },
  { label: 'Horizon', key: 'horizon' },
]

/** Human labels for each filter that is set, for the active filters summary. */
function activeLabels(filters: IdeaFilters): string[] {
  const out: string[] = []
  if (filters.search.trim()) out.push(`Search: ${filters.search.trim()}`)
  for (const f of [...FACETS, ...LEGACY]) {
    const v = filters[f.key] as string
    if (v !== ALL) out.push(`${f.label}: ${v}`)
  }
  for (const t of filters.tags) out.push(`Tag: ${t}`)
  return out
}

const FACETS: { label: string; facet: FacetKey; key: keyof IdeaFilters }[] = [
  { label: 'Category', facet: 'strategyCategory', key: 'strategyCategory' },
  { label: 'Trading style', facet: 'tradingStyle', key: 'tradingStyle' },
  { label: 'Direction', facet: 'direction', key: 'direction' },
  { label: 'Market', facet: 'market', key: 'market' },
  { label: 'Timeframe', facet: 'timeframe', key: 'timeframe' },
  { label: 'Research state', facet: 'researchState', key: 'researchState' },
  { label: 'Verdict', facet: 'verdict', key: 'verdict' },
]

interface StrategyFiltersProps {
  ideas: IdeaCard[]
  filters: IdeaFilters
  onChange: (next: IdeaFilters) => void
  shown: number
}

export function StrategyFilters({ ideas, filters, onChange, shown }: StrategyFiltersProps) {
  const set = (patch: Partial<IdeaFilters>) => onChange({ ...filters, ...patch })
  const facetOpts = useMemo(
    () => FACETS.map((f) => facetOptionsFor(ideas, filters, f.facet)),
    [ideas, filters]
  )
  const legacyOpts = useMemo(
    () => LEGACY.map((f) => legacyOptionsFor(ideas, filters, f.key)),
    [ideas, filters]
  )
  const tags = useMemo(() => tagOptionsFor(ideas, filters), [ideas, filters])
  const active = useMemo(() => activeLabels(filters), [filters])
  const toggleTag = (tag: string) => {
    const has = filters.tags.some((t) => t.toLowerCase() === tag.toLowerCase())
    set({
      tags: has
        ? filters.tags.filter((t) => t.toLowerCase() !== tag.toLowerCase())
        : [...filters.tags, tag],
    })
  }
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-3">
        <div className="text-sm flex flex-col gap-1">
          <label htmlFor="strategy-search">Search</label>
          <div className="flex items-center gap-2">
            <Input
              id="strategy-search"
              value={filters.search}
              placeholder="Name, category or tag"
              onChange={(e) => set({ search: e.target.value })}
              className="w-56"
            />
            <button
              type="button"
              className="text-sm underline underline-offset-4 disabled:no-underline disabled:text-muted-foreground"
              disabled={!filters.search}
              onClick={() => set({ search: '' })}
            >
              Clear search
            </button>
          </div>
        </div>
        {FACETS.map((f, i) => (
          <FilterSelect
            key={f.key}
            label={f.label}
            value={filters[f.key] as string}
            options={facetOpts[i]}
            onChange={(v) => set({ [f.key]: v } as Partial<IdeaFilters>)}
          />
        ))}
        {LEGACY.map((f, i) => (
          <FilterSelect
            key={f.key}
            label={f.label}
            value={filters[f.key]}
            options={legacyOpts[i]}
            onChange={(v) => set({ [f.key]: v } as Partial<IdeaFilters>)}
          />
        ))}
      </div>
      {tags.length > 0 && (
        <fieldset className="flex flex-wrap items-center gap-2">
          <legend className="text-sm mb-1">Has all selected tags</legend>
          {tags.map((t) => {
            const on = filters.tags.some((s) => s.toLowerCase() === t.value.toLowerCase())
            return (
              <button
                key={t.value}
                type="button"
                aria-pressed={on}
                onClick={() => toggleTag(t.value)}
                className={cn(
                  'rounded-full border px-2 py-0.5 text-xs',
                  on ? 'bg-primary text-primary-foreground' : 'text-muted-foreground'
                )}
              >
                {`${t.value} (${t.count})`}
              </button>
            )
          })}
        </fieldset>
      )}
      {active.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 text-sm" data-testid="active-filters">
          <span>{`Active filters: ${active.join('; ')}`}</span>
          <button
            type="button"
            className="underline underline-offset-4"
            onClick={() => onChange({ ...EMPTY_FILTERS, tags: [] })}
          >
            Clear all filters
          </button>
        </div>
      )}
      <p className="text-xs text-muted-foreground" data-testid="results-count">
        {`${shown} of ${ideas.length} strategies`}
      </p>
    </div>
  )
}
