import { AlertTriangle, CheckCircle2, CircleHelp, Info, XCircle } from 'lucide-react'
import type { ReactNode } from 'react'

/**
 * One colour language for the operations pages.
 * good = healthy / verified, warn = warning / degraded / pending, bad = critical / failed,
 * info = informational / in progress, idle = unknown / unverified / not run.
 * Every badge carries a text label, so colour is never the only signal.
 */
export type Tone = 'good' | 'warn' | 'bad' | 'info' | 'idle'

export const TONE_STYLE: Record<Tone, string> = {
  good: 'border-green-600/40 bg-green-600/15 text-green-600 dark:text-green-400',
  warn: 'border-amber-500/40 bg-amber-500/15 text-amber-600 dark:text-amber-400',
  bad: 'border-red-600/40 bg-red-600/15 text-red-600 dark:text-red-400',
  info: 'border-blue-500/40 bg-blue-500/15 text-blue-600 dark:text-blue-400',
  idle: 'border-border bg-muted text-muted-foreground',
}

export const TONE_TEXT: Record<Tone, string> = {
  good: 'text-green-600 dark:text-green-400',
  warn: 'text-amber-600 dark:text-amber-400',
  bad: 'text-red-600 dark:text-red-400',
  info: 'text-blue-600 dark:text-blue-400',
  idle: 'text-muted-foreground',
}

export function ToneIcon({ tone, className = 'h-4 w-4' }: { tone: Tone; className?: string }) {
  const cls = `${className} ${TONE_TEXT[tone]}`
  if (tone === 'good') return <CheckCircle2 className={cls} aria-hidden="true" />
  if (tone === 'bad') return <XCircle className={cls} aria-hidden="true" />
  if (tone === 'warn') return <AlertTriangle className={cls} aria-hidden="true" />
  if (tone === 'info') return <Info className={cls} aria-hidden="true" />
  return <CircleHelp className={cls} aria-hidden="true" />
}

export function StatusBadge({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <span
      className={`inline-flex items-center gap-1 whitespace-nowrap rounded-md border px-2 py-0.5 text-[13px] font-medium ${TONE_STYLE[tone]}`}
    >
      <ToneIcon tone={tone} className="h-3 w-3" />
      {children}
    </span>
  )
}
