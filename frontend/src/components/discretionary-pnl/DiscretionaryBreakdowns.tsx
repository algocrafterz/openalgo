import type { DiscretionarySummary } from '@/api/discretionaryPnl'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { BREAKDOWN_TABS, type BreakdownTabKey, breakdownTabRows } from '@/lib/discretionaryPnlView'
import { HowYouTradeTable } from './DiscretionaryInsights'
import { BreakdownTable } from './DiscretionaryTables'

type Money = (value: number) => string

/**
 * "Where and when you make or lose money": one card with every breakdown shown at once, stacked
 * (symbol, long / short, time of day, weekday, holding time, then product and intraday vs
 * overnight). A section appears only when it has two or more groups to compare (see
 * breakdownVisibility). Each section keeps the table it always had, with its trade count (the
 * sample), win rate and gross / net columns. Wide tables scroll inside their own container.
 */
export function DiscretionaryBreakdowns({
  summary,
  tabs,
  formatCurrency,
}: {
  summary: DiscretionarySummary
  /** Sections with something to compare, in order. */
  tabs: BreakdownTabKey[]
  formatCurrency: Money
}) {
  const sections = BREAKDOWN_TABS.filter((o) => tabs.includes(o.key))
  if (sections.length === 0) return null
  return (
    <Card data-testid="breakdowns-card">
      <CardHeader>
        <CardTitle>Where and when you make or lose money</CardTitle>
      </CardHeader>
      <CardContent className="space-y-6">
        {sections.map((o) => (
          <section
            key={o.key}
            data-testid={`breakdown-section-${o.key}`}
            aria-labelledby={`breakdown-heading-${o.key}`}
            className="min-w-0 space-y-2"
          >
            <h3 id={`breakdown-heading-${o.key}`} className="text-sm font-medium">
              {o.label}
            </h3>
            {o.table === 'how' ? (
              <HowYouTradeTable
                rows={breakdownTabRows(summary, o.key)}
                column={o.column}
                formatCurrency={formatCurrency}
              />
            ) : (
              <BreakdownTable
                firstColumn={o.column}
                rows={breakdownTabRows(summary, o.key)}
                formatCurrency={formatCurrency}
              />
            )}
            <p className="text-xs text-muted-foreground" title={o.detail}>
              {o.note}
            </p>
          </section>
        ))}
        <p className="text-xs text-muted-foreground">
          Net is after estimated charges. Trades is the sample size.
        </p>
      </CardContent>
    </Card>
  )
}
