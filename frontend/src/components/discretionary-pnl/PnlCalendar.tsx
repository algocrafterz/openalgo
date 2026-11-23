import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { ChevronLeft, ChevronRight, Loader2 } from 'lucide-react'
import { useMemo, useState } from 'react'
import {
  type DiscretionaryCategory,
  type DiscretionaryFilters,
  discretionaryPnlApi,
} from '@/api/discretionaryPnl'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  buildMonthCalendar,
  type CalendarDay,
  canGoNext,
  canGoPrevious,
  monthKeyOf,
  monthRange,
  monthTitle,
  shiftMonth,
} from '@/lib/discretionaryCalendar'
import { DISCRETIONARY_POLL_MS, fmtNetMoney, pnlTextClass } from '@/lib/discretionaryPnlView'
import { cn } from '@/lib/utils'

type Money = (value: number) => string

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

const CELL_TONE: Record<CalendarDay['status'], string> = {
  profit: 'border-green-600/40 bg-green-600/15',
  loss: 'border-red-600/40 bg-red-600/15',
  flat: 'border-border bg-muted/40',
  none: 'border-transparent bg-muted/10 text-muted-foreground',
  unavailable: 'border-dashed border-amber-500/60 bg-amber-500/10',
  untracked: 'border-transparent bg-transparent text-muted-foreground/50',
}

function dayLabel(day: CalendarDay, money: Money): string {
  if (day.future) return `${day.date}: not yet`
  if (day.net === null) {
    if (day.status === 'untracked') return `${day.date}: before the tracked history`
    return day.status === 'unavailable'
      ? `${day.date}: incomplete data. ${day.note ?? ''}`.trim()
      : `${day.date}: no trades`
  }
  const base = `${day.date}: net ${money(day.net)} after estimated charges, ${day.trades} completed trade(s), ${day.wins} win(s), ${day.losses} loss(es)`
  return day.note ? `${base}. Incomplete: ${day.note}` : base
}

function DayCell({
  day,
  selected,
  money,
  onSelect,
}: {
  day: CalendarDay
  selected: boolean
  money: Money
  onSelect: (date: string) => void
}) {
  const label = dayLabel(day, money)
  return (
    <button
      type="button"
      disabled={day.future}
      onClick={() => onSelect(day.date)}
      aria-label={label}
      aria-pressed={selected}
      title={label}
      data-testid={`calendar-day-${day.date}`}
      data-status={day.status}
      data-incomplete={day.incomplete ? 'true' : 'false'}
      className={cn(
        'flex min-h-14 min-w-0 flex-col justify-between rounded-md border p-1 text-left text-[11px] leading-tight transition-colors',
        'hover:ring-1 hover:ring-ring focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        'disabled:cursor-default disabled:opacity-40 disabled:hover:ring-0',
        CELL_TONE[day.status],
        selected && 'ring-2 ring-primary'
      )}
    >
      <span className="text-muted-foreground">{day.day}</span>
      {day.net !== null ? (
        <span className="min-w-0">
          <span
            className={cn('block truncate font-mono text-xs font-semibold', pnlTextClass(day.net))}
          >
            {money(day.net)}
            {day.incomplete ? '*' : ''}
          </span>
          <span className="block truncate text-[10px] text-muted-foreground">
            {day.trades} {day.trades === 1 ? 'trade' : 'trades'}
            {day.trades > 0 ? ` · ${day.wins}W ${day.losses}L` : ' · partial exit'}
          </span>
        </span>
      ) : day.status === 'unavailable' ? (
        <span className="text-[10px] text-amber-700 dark:text-amber-500">Incomplete</span>
      ) : null}
    </button>
  )
}

/**
 * Monthly P&L calendar: realised net P&L after estimated charges for each day, taken from the
 * daily rows of the existing summary request with the month as its range (same broker, account,
 * symbol and category filters as the report). Open positions and unrealised P&L are not in it.
 * Selecting a day makes it the report date.
 */
export function PnlCalendar({
  anchorDate,
  selectedRange,
  today,
  filters,
  category,
  reliableSince,
  formatCurrency,
  onSelectDate,
}: {
  /** A date of the report on screen; the calendar shows its month, and follows it. */
  anchorDate: string
  selectedRange: { start: string; end: string } | undefined
  today: string
  filters: DiscretionaryFilters
  category: DiscretionaryCategory
  reliableSince?: string
  formatCurrency: Money
  onSelectDate: (date: string) => void
}) {
  const clamp = (date: string) => {
    const key = monthKeyOf(date)
    return key > monthKeyOf(today) ? monthKeyOf(today) : key
  }
  const [month, setMonth] = useState(() => clamp(anchorDate))
  // The report date moved to another month (period change, a click elsewhere): follow it. Only
  // a new anchor date moves the month, so the month the trader browsed to stays otherwise.
  const [seenAnchor, setSeenAnchor] = useState(anchorDate)
  if (seenAnchor !== anchorDate) {
    setSeenAnchor(anchorDate)
    setMonth(clamp(anchorDate))
  }

  const range = useMemo(() => monthRange(month, today), [month, today])
  const query = useQuery({
    queryKey: ['discretionary-pnl', 'calendar', month, filters, category],
    queryFn: () => discretionaryPnlApi.summary('custom', range, filters, false, category),
    refetchInterval: DISCRETIONARY_POLL_MS,
    placeholderData: keepPreviousData,
    retry: false,
  })
  const data = query.data
  // A month still loading must not show the previous month's days under the new title.
  const shown = data && !query.isPlaceholderData ? data : undefined
  const calendar = useMemo(
    () => (shown ? buildMonthCalendar(shown, month, today) : null),
    [shown, month, today]
  )
  const selected =
    selectedRange && selectedRange.start === selectedRange.end ? selectedRange.start : null
  const totals = calendar?.totals

  return (
    <Card data-testid="pnl-calendar" className="gap-3 py-4">
      <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2 pb-0">
        <CardTitle className="text-base">P&amp;L calendar</CardTitle>
        <div className="flex items-center gap-1">
          <Button
            type="button"
            variant="outline"
            size="icon"
            className="size-8"
            aria-label="Previous month"
            disabled={!canGoPrevious(month, reliableSince)}
            title={
              canGoPrevious(month, reliableSince)
                ? undefined
                : 'Nothing is tracked before this month'
            }
            onClick={() => setMonth((m) => shiftMonth(m, -1))}
          >
            <ChevronLeft className="size-4" />
          </Button>
          <span
            className="min-w-32 text-center text-sm font-medium"
            data-testid="calendar-month"
            aria-live="polite"
          >
            {monthTitle(month)}
          </span>
          <Button
            type="button"
            variant="outline"
            size="icon"
            className="size-8"
            aria-label="Next month"
            disabled={!canGoNext(month, today)}
            onClick={() => setMonth((m) => shiftMonth(m, 1))}
          >
            <ChevronRight className="size-4" />
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {query.isError && !calendar ? (
          <div className="flex flex-wrap items-center gap-3 text-sm" data-testid="calendar-error">
            <span>Could not load this month. Your report below is not affected.</span>
            <Button type="button" size="sm" variant="outline" onClick={() => query.refetch()}>
              Retry
            </Button>
          </div>
        ) : !calendar || !totals ? (
          <div className="flex items-center justify-center py-8" data-testid="calendar-loading">
            <Loader2 className="size-6 animate-spin" />
          </div>
        ) : (
          <>
            <dl
              className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-4"
              data-testid="calendar-month-totals"
            >
              <div className="min-w-0 rounded-md border px-2 py-1.5">
                <dt className="text-muted-foreground">Month net P&amp;L</dt>
                <dd
                  className={cn(
                    'truncate font-mono text-sm font-semibold',
                    pnlTextClass(totals.net)
                  )}
                  data-testid="calendar-total-net"
                  title="Realised P&L after estimated charges, summed over the days shown. Open positions are not included."
                >
                  {fmtNetMoney(totals.net, formatCurrency)}
                  {totals.incompleteDays > 0 ? '*' : ''}
                </dd>
              </div>
              <div className="rounded-md border px-2 py-1.5">
                <dt className="text-muted-foreground">Trading days</dt>
                <dd className="font-mono text-sm font-semibold" data-testid="calendar-trading-days">
                  {totals.tradingDays}
                </dd>
              </div>
              <div className="rounded-md border px-2 py-1.5">
                <dt className="text-muted-foreground">Profitable days</dt>
                <dd
                  className="font-mono text-sm font-semibold text-green-700 dark:text-green-600"
                  data-testid="calendar-profitable-days"
                >
                  {totals.profitableDays}
                </dd>
              </div>
              <div className="rounded-md border px-2 py-1.5">
                <dt className="text-muted-foreground">Losing days</dt>
                <dd
                  className="font-mono text-sm font-semibold text-red-600 dark:text-red-400"
                  data-testid="calendar-losing-days"
                >
                  {totals.losingDays}
                </dd>
              </div>
            </dl>

            <div className="overflow-x-auto">
              <div className="min-w-[420px] space-y-1">
                <div className="grid grid-cols-7 gap-1 text-center text-[11px] text-muted-foreground">
                  {WEEKDAYS.map((w) => (
                    <div key={w}>{w}</div>
                  ))}
                </div>
                {calendar.weeks.map((week) => (
                  <div
                    key={week.find((d) => d)?.date ?? 'blank'}
                    className="grid grid-cols-7 gap-1"
                  >
                    {week.map((day, i) =>
                      day ? (
                        <DayCell
                          key={day.date}
                          day={day}
                          selected={day.date === selected}
                          money={formatCurrency}
                          onSelect={onSelectDate}
                        />
                      ) : (
                        <div key={`pad-${WEEKDAYS[i]}`} aria-hidden="true" />
                      )
                    )}
                  </div>
                ))}
              </div>
            </div>

            <p className="text-xs text-muted-foreground" data-testid="calendar-legend">
              Realised P&amp;L after estimated charges, by the day a trade was closed. Open
              positions are not included. Grey days had no trades. Dashed amber days may have
              missing data.
              {totals.incompleteDays > 0
                ? ' * means part of the figure is missing (some trades have no charge estimate or were not collected).'
                : ''}{' '}
              Select a day to open its report.
            </p>
          </>
        )}
      </CardContent>
    </Card>
  )
}
