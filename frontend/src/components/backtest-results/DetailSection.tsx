import { type ReactNode, useEffect, useRef, useState } from 'react'

/**
 * A collapsed group of detailed evidence below the decision summary. Its content stays mounted
 * (only hidden), and it opens by itself when a link targets something inside it (`#gate-G3.2`)
 * or when `forceOpen` turns true (the trade drill-down), so following a reference never lands
 * on hidden content.
 */
export function DetailSection({
  id,
  title,
  summary,
  defaultOpen = false,
  forceOpen = false,
  children,
}: {
  id: string
  title: string
  summary?: string
  defaultOpen?: boolean
  forceOpen?: boolean
  children: ReactNode
}) {
  const ref = useRef<HTMLDetailsElement>(null)
  const [open, setOpen] = useState(defaultOpen)

  // Opens when either turns true, which may happen after the first render (data still loading).
  useEffect(() => {
    if (forceOpen || defaultOpen) setOpen(true)
  }, [forceOpen, defaultOpen])

  useEffect(() => {
    const reveal = () => {
      const target = decodeURIComponent(window.location.hash.slice(1))
      const el = target ? document.getElementById(target) : null
      if (el && ref.current?.contains(el)) {
        setOpen(true)
        requestAnimationFrame(() => el.scrollIntoView?.({ block: 'start' }))
      }
    }
    reveal()
    window.addEventListener('hashchange', reveal)
    return () => window.removeEventListener('hashchange', reveal)
  }, [])

  return (
    <details
      ref={ref}
      open={open}
      onToggle={(e) => setOpen(e.currentTarget.open)}
      className="rounded-md border"
      data-testid={`section-${id}`}
    >
      <summary className="cursor-pointer select-none p-3">
        <span className="font-medium">{title}</span>
        {summary && <span className="ml-2 text-sm text-muted-foreground">{summary}</span>}
      </summary>
      <div className="space-y-6 border-t p-3">{children}</div>
    </details>
  )
}
