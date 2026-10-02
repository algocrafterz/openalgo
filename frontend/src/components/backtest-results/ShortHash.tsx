import { useState } from 'react'

const BTN = 'ml-1 text-xs underline text-muted-foreground'

/**
 * First eight characters of a hash with the full value on hover, and a button that toggles the
 * full text (keyboard and touch reachable) plus a copy button when the clipboard is available.
 * Tolerates a missing value.
 */
export function ShortHash({ value }: { value: string | null | undefined }) {
  const [full, setFull] = useState(false)
  if (!value) return <span className="text-muted-foreground italic">not captured</span>
  const canCopy = typeof navigator !== 'undefined' && !!navigator.clipboard?.writeText
  return (
    <span>
      <span title={value} className={full ? 'font-mono break-all' : 'font-mono'}>
        {full ? value : value.slice(0, 8)}
      </span>
      <button type="button" className={BTN} onClick={() => setFull((f) => !f)}>
        {full ? 'Hide full value' : 'Show full value'}
      </button>
      {canCopy && (
        <button
          type="button"
          className={BTN}
          onClick={() => {
            void navigator.clipboard.writeText(value).catch(() => undefined)
          }}
        >
          Copy full value
        </button>
      )}
    </span>
  )
}
