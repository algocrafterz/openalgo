import { useMemo, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet'
import { GLOSSARY, GLOSSARY_IDS, type GlossaryEntry } from '@/lib/backtestGlossary'

function groupByCategory(query: string): [string, GlossaryEntry[]][] {
  const q = query.trim().toLowerCase()
  const groups = new Map<string, GlossaryEntry[]>()
  for (const id of GLOSSARY_IDS) {
    const e = GLOSSARY[id]
    if (q && !`${e.term} ${e.short} ${e.long}`.toLowerCase().includes(q)) continue
    groups.set(e.category, [...(groups.get(e.category) ?? []), e])
  }
  return [...groups.entries()]
}

/** Help button that opens a searchable side sheet listing every glossary term. */
export function GlossaryDrawer() {
  const [query, setQuery] = useState('')
  const groups = useMemo(() => groupByCategory(query), [query])

  return (
    <Sheet onOpenChange={(open) => !open && setQuery('')}>
      <SheetTrigger asChild>
        <Button variant="outline" size="sm">
          Glossary
        </Button>
      </SheetTrigger>
      <SheetContent side="right" className="overflow-y-auto sm:max-w-md">
        <SheetHeader>
          <SheetTitle>Backtest Glossary</SheetTitle>
          <SheetDescription>
            Plain-language meanings of the terms used on this page.
          </SheetDescription>
        </SheetHeader>
        <div className="px-4">
          <Input
            type="search"
            aria-label="Search glossary"
            placeholder="Search terms"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <div className="space-y-5 px-4 pb-6">
          {groups.length === 0 && (
            <p className="text-muted-foreground text-sm">No matching terms.</p>
          )}
          {groups.map(([category, entries]) => (
            <section key={category}>
              <h3 className="text-muted-foreground mb-2 text-xs font-semibold uppercase">
                {category}
              </h3>
              <dl className="space-y-3">
                {entries.map((e) => (
                  <div key={e.id}>
                    <dt className="text-sm font-medium">{e.term}</dt>
                    <dd className="text-muted-foreground text-sm">{e.long}</dd>
                  </div>
                ))}
              </dl>
            </section>
          ))}
        </div>
      </SheetContent>
    </Sheet>
  )
}
