import * as CheckboxPrimitive from '@radix-ui/react-checkbox'
import { Check, ChevronDown, Minus } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import {
  clearAll,
  cycleStrategy,
  isWholePortfolio,
  resolveStrategies,
  type StrategyFilter,
  selectAll,
  strategyState,
  WHOLE_PORTFOLIO,
} from '@/lib/strategyFilter'
import { cn } from '@/lib/utils'

interface StrategyFilterPickerProps {
  filter: StrategyFilter
  /** Every strategy that can be chosen. */
  names: string[]
  onChange: (filter: StrategyFilter) => void
}

// The server accepts at most this many names in one request.
const MAX_NAMES = 100

const STATE_LABEL = { include: 'Included', exclude: 'Excluded', neutral: '' } as const

function summary(filter: StrategyFilter, names: string[]): string {
  if (filter.none) return 'No strategy selected'
  if (isWholePortfolio(filter)) return 'Portfolio (all strategies)'
  const chosen = resolveStrategies(filter, names)
  if (names.length > 0 && chosen.length === 0) return 'No strategy selected'
  if (chosen.length === 1) return chosen[0]
  if (names.length === 0) return 'Strategies'
  return `${chosen.length} of ${names.length} strategies`
}

/**
 * Picks which strategies the report covers. Each row steps through Included,
 * Excluded and not chosen. With nothing included, every strategy is in except
 * the excluded ones, so a strategy that starts trading later is picked up.
 */
export function StrategyFilterPicker({ filter, names, onChange }: StrategyFilterPickerProps) {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button variant="outline" className="w-[240px] justify-between font-normal">
          <span className="truncate">{summary(filter, names)}</span>
          <ChevronDown className="h-4 w-4 shrink-0 opacity-50" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-[300px] p-3">
        <div className="flex flex-wrap gap-2 mb-3">
          <Button
            variant="outline"
            size="sm"
            disabled={names.length > MAX_NAMES}
            onClick={() => onChange(selectAll(names))}
          >
            Select All
          </Button>
          <Button variant="outline" size="sm" onClick={() => onChange(clearAll())}>
            Clear All
          </Button>
          <Button variant="ghost" size="sm" onClick={() => onChange(WHOLE_PORTFOLIO)}>
            Reset
          </Button>
        </div>
        {names.length > MAX_NAMES && (
          <p className="text-xs text-muted-foreground mb-2">
            Too many strategies to select at once. Use Reset to show the whole portfolio.
          </p>
        )}
        <p className="text-xs text-muted-foreground mb-2">
          Click a strategy to include it, click again to exclude it, and once more to clear it. With
          none included, all strategies are shown except the excluded ones.
        </p>
        <div className="max-h-64 overflow-y-auto space-y-1">
          {names.length === 0 && (
            <p className="text-sm text-muted-foreground py-2">
              {filter.none
                ? 'Select at least one strategy.'
                : 'No strategies with closed trades yet.'}
            </p>
          )}
          {names.map((name) => {
            const state = strategyState(filter, name)
            return (
              <CheckboxPrimitive.Root
                key={name}
                checked={state === 'include' ? true : state === 'exclude' ? 'indeterminate' : false}
                onCheckedChange={() => onChange(cycleStrategy(filter, name))}
                className="flex w-full items-center gap-2 rounded px-1 py-1 text-left text-sm cursor-pointer hover:bg-muted/50"
              >
                <span
                  aria-hidden="true"
                  className={cn(
                    'flex h-4 w-4 shrink-0 items-center justify-center rounded-sm border border-primary',
                    state === 'include' && 'bg-primary text-primary-foreground',
                    state === 'exclude' &&
                      'border-destructive bg-destructive text-destructive-foreground'
                  )}
                >
                  <CheckboxPrimitive.Indicator className="flex items-center justify-center">
                    {state === 'exclude' ? (
                      <Minus className="h-3 w-3" />
                    ) : (
                      <Check className="h-3 w-3" />
                    )}
                  </CheckboxPrimitive.Indicator>
                </span>
                <span className="flex-1 truncate">{name}</span>
                <span
                  className={cn(
                    'text-xs',
                    state === 'exclude' ? 'text-red-600' : 'text-muted-foreground'
                  )}
                >
                  {STATE_LABEL[state]}
                </span>
              </CheckboxPrimitive.Root>
            )
          })}
        </div>
      </PopoverContent>
    </Popover>
  )
}
