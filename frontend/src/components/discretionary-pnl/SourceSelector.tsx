import { Button } from '@/components/ui/button'
import type { PageSource } from '@/lib/discretionaryPnlView'

const OPTIONS: { value: PageSource; label: string; title: string }[] = [
  {
    value: 'strategies',
    label: 'Strategies (current mode)',
    title: 'Strategy trades in the app mode (Live or Analyze). Follows the global mode switch.',
  },
  {
    value: 'discretionary',
    label: 'Discretionary',
    title: 'Trades you placed yourself at the broker, rebuilt from the fill ledger.',
  },
]

/**
 * Chooses what the Performance page shows. Local to the page: it never changes the app's
 * Live/Analyze mode. Renders nothing unless the server has the Discretionary source enabled.
 */
export function SourceSelector({
  enabled,
  value,
  onChange,
}: {
  enabled: boolean
  value: PageSource
  onChange: (next: PageSource) => void
}) {
  if (!enabled) return null
  return (
    <fieldset
      aria-label="Performance source"
      className="m-0 inline-flex min-w-0 items-center gap-1 border-0 p-0"
    >
      {OPTIONS.map((option) => (
        <Button
          key={option.value}
          type="button"
          size="sm"
          variant={value === option.value ? 'default' : 'outline'}
          aria-pressed={value === option.value}
          title={option.title}
          onClick={() => {
            if (value !== option.value) onChange(option.value)
          }}
        >
          {option.label}
        </Button>
      ))}
    </fieldset>
  )
}
