import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { getTerm } from '@/lib/backtestGlossary'

interface InfoTipProps {
  term: string
}

/** Small help trigger that shows the short glossary definition on hover or focus. */
export function InfoTip({ term }: InfoTipProps) {
  const entry = getTerm(term)
  if (!entry) return null
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-label={`What is ${entry.term}?`}
          className="text-muted-foreground hover:text-foreground focus-visible:ring-ring ml-1 inline-flex size-4 items-center justify-center rounded-full border text-[10px] leading-none font-medium focus-visible:ring-2 focus-visible:outline-hidden"
        >
          ?
        </button>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs">{entry.short}</TooltipContent>
    </Tooltip>
  )
}
