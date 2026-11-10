/**
 * What a backtest is set up with, in two parts: the few things a trader decides before pressing
 * Run, and everything else the run rests on.
 *
 * **Essentials are the eight things a run is of:** the strategy, the instrument, the timeframe,
 * the date range, the starting capital, the position size, the commission and the slippage. The
 * trader changes the strategy and the dates (and the script's own inputs, which sit with them).
 * The instrument and timeframe are the chart's, and the capital, size, commission and slippage
 * are the script's: those are shown with whose they are, because a box that does nothing is
 * worse than no box (see `backtestInputs.ts` for what a host may and may not set).
 *
 * **Advanced is everything else the script declares about how it trades.** It is closed by
 * default and holds nothing a trader needs in order to run a sensible backtest.
 */

import type { ReactNode } from 'react'
import type { DeclaredSettings, InputDeclaration } from '@/lib/trading/backtestInputs'
import {
  advancedFacts,
  costsNote,
  rangeNote,
  type SetupFact,
  SOURCE_LABEL,
} from '@/lib/trading/backtestSetup'
import type { StoredScript } from '@/lib/trading/openscriptFiles'
import { StrategyInputs } from './StrategyInputs'

const LABEL = 'text-[10px] uppercase tracking-wide text-muted-foreground'
const FIELD = 'h-8 rounded border border-border bg-background px-2 text-xs'

function Fact({ fact }: { fact: SetupFact }) {
  return (
    <div className="flex flex-col gap-0.5" data-testid={`setup-${fact.key}`}>
      <div className="flex items-baseline gap-2 text-[11px]">
        <span className="text-muted-foreground">{fact.label}</span>
        <span className="ml-auto truncate font-mono tabular-nums">{fact.value}</span>
        <span
          className="w-[86px] shrink-0 text-right text-[9px] uppercase tracking-wide text-muted-foreground"
          data-testid={`setup-${fact.key}-source`}
        >
          {SOURCE_LABEL[fact.source]}
        </span>
      </div>
      {fact.hint && <span className="text-[10px] text-muted-foreground">{fact.hint}</span>}
    </div>
  )
}

interface EssentialsProps {
  scripts: StoredScript[]
  file: string
  onFile(file: string): void
  from: string
  to: string
  onFrom(value: string): void
  onTo(value: string): void
  /** Whether the dates are the trader's. When not, they follow the timeframe. */
  datesAreTheirs: boolean
  onResetDates(): void
  interval: string
  chart: SetupFact[]
  script: SetupFact[]
  declared: DeclaredSettings | null
  declarations: readonly InputDeclaration[]
  edited: Readonly<Record<string, string>>
  onEdit(key: string, value: string): void
}

export function SetupEssentials(p: EssentialsProps) {
  const [instrument, timeframe] = p.chart
  const note = costsNote(p.declared)
  return (
    <section
      className="flex flex-col gap-2"
      data-testid="backtest-essentials"
      aria-label="Backtest essentials"
    >
      <label className="flex flex-col gap-1">
        <span className={LABEL}>Strategy</span>
        <select
          className={FIELD}
          value={p.file}
          onChange={(e) => p.onFile(e.target.value)}
          disabled={p.scripts.length === 0}
        >
          {p.scripts.length === 0 && <option value="">No strategies saved</option>}
          {p.scripts.map((one) => (
            <option key={one.file} value={one.file}>
              {one.file}
            </option>
          ))}
        </select>
      </label>

      <div className="flex flex-col gap-1 rounded border border-border p-2">
        <Fact fact={instrument} />
        <Fact fact={timeframe} />
      </div>

      <div className="flex flex-col gap-1">
        <div className="grid grid-cols-2 gap-2">
          <label className="flex flex-col gap-1">
            <span className={LABEL}>From</span>
            <input
              type="date"
              className={FIELD}
              value={p.from}
              max={p.to}
              onChange={(e) => p.onFrom(e.target.value)}
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className={LABEL}>To</span>
            <input
              type="date"
              className={FIELD}
              value={p.to}
              min={p.from}
              onChange={(e) => p.onTo(e.target.value)}
            />
          </label>
        </div>
        <p className="text-[10px] text-muted-foreground" data-testid="setup-range-note">
          {rangeNote(p.interval, p.datesAreTheirs)}
          {p.datesAreTheirs && p.interval && (
            <>
              {' '}
              <button type="button" className="underline" onClick={p.onResetDates}>
                Use the default range
              </button>
            </>
          )}
        </p>
      </div>

      <div className="flex flex-col gap-1 rounded border border-border p-2">
        {p.script.map((fact) => (
          <Fact key={fact.key} fact={fact} />
        ))}
        {note && (
          <p
            className="pt-1 text-[10px] leading-relaxed text-muted-foreground"
            data-testid="setup-costs-note"
          >
            {note}
          </p>
        )}
      </div>

      {p.declarations.length > 0 && (
        <StrategyInputs
          declarations={p.declarations}
          edited={p.edited}
          onChange={p.onEdit}
          note="A box left empty uses the script's own default. Run again to apply a change. These are for testing on this chart only and never reach a strategy that is running live: set those under Strategies."
        />
      )}
      {p.declared && p.declarations.length === 0 && (
        <p
          className="text-[10px] leading-relaxed text-muted-foreground"
          data-testid="setup-no-inputs"
        >
          This script declares no inputs, so there is nothing to change here. Its numbers are
          written into it: to make one adjustable, give it a name with input(), as in length =
          input(9, "Fast length", min = 1).
        </p>
      )}
    </section>
  )
}

interface AdvancedProps {
  open: boolean
  onToggle(): void
  declared: DeclaredSettings
  /** Where the order size comes from, in words (from the language, not decided here). */
  sizeNote: ReactNode
}

export function SetupAdvanced({ open, onToggle, declared, sizeNote }: AdvancedProps) {
  return (
    <div className="rounded border border-border" data-testid="backtest-advanced">
      <button
        type="button"
        className="flex h-7 w-full items-center px-2 text-[10px] uppercase tracking-wide text-muted-foreground hover:bg-accent/50"
        onClick={onToggle}
        aria-expanded={open}
      >
        Advanced settings
        <span className="ml-auto">{open ? 'hide' : 'show'}</span>
      </button>

      {open && (
        <div className="flex flex-col gap-2 border-t border-border p-2">
          <div className="flex flex-col gap-1">
            <span className={LABEL}>How the script trades</span>
            <dl className="grid grid-cols-2 gap-x-2 gap-y-0.5 font-mono text-[10px] text-muted-foreground">
              {advancedFacts(declared).map((row) => (
                <div key={row.label} className="contents">
                  <dt>{row.label}</dt>
                  <dd className="text-right">{row.value}</dd>
                </div>
              ))}
            </dl>
          </div>
          {sizeNote && (
            <div className="flex flex-col gap-1" data-testid="setup-size-note">
              <span className={LABEL}>Position size</span>
              <p className="text-[10px] leading-relaxed text-muted-foreground">{sizeNote}</p>
            </div>
          )}
          <p className="text-[10px] leading-relaxed text-muted-foreground">
            These are the script's own, set in its strategy() line, and are shown rather than
            offered: a commission supplied here beside a declared one describes the same money twice
            and is refused before the first bar. Edit the script to change them.
          </p>
        </div>
      )}
    </div>
  )
}
