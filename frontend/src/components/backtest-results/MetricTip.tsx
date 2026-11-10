import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'

/**
 * One-line definitions with units for the figures a trader reads first. They sit beside the
 * glossary (GlossaryDrawer keeps the long wording); each says what the number is measured in.
 */
const METRIC_TIPS = {
  profit_factor: {
    name: 'Profit factor',
    text: 'Total profit of winning trades divided by total loss of losing trades, after costs. A ratio with no unit. Above 1.0 means winners outweighed losers; 1.3 means Rs 1.30 won for every Rs 1 lost.',
  },
  expectancy: {
    name: 'Expectancy',
    text: 'Average result of one trade after costs. Shown in basis points of the position value (100 bps = 1%) or in R (multiples of the amount risked on the trade).',
  },
  max_drawdown: {
    name: 'Max drawdown',
    text: 'Largest fall from a peak to a later low of the equity curve, in percent of that peak. Where only trade results exist it is in R (multiples of the amount risked).',
  },
  sharpe: {
    name: 'Sharpe ratio',
    text: 'Average daily return divided by the variability of daily returns, scaled to a year. A ratio with no unit. Unreliable on less than a year of data.',
  },
  slippage: {
    name: 'Slippage',
    text: 'Assumed gap between the price the signal expected and the price the order fills at, in basis points per side (100 bps = 1%). It is charged on entry and again on exit.',
  },
  win_rate: {
    name: 'Win rate',
    text: 'Share of trades that ended with a profit after costs, in percent. A high win rate alone says nothing about profit if the losses are larger than the wins.',
  },
} as const

export type MetricTipId = keyof typeof METRIC_TIPS

/** Small help trigger with the definition and unit of a figure. */
export function MetricTip({ metric }: { metric: MetricTipId }) {
  const entry = METRIC_TIPS[metric]
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-label={`What is ${entry.name}?`}
          className="text-muted-foreground hover:text-foreground focus-visible:ring-ring ml-1 inline-flex size-4 items-center justify-center rounded-full border text-[10px] leading-none font-medium focus-visible:ring-2 focus-visible:outline-hidden"
        >
          ?
        </button>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs">{entry.text}</TooltipContent>
    </Tooltip>
  )
}
