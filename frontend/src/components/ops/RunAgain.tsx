import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, Play, X } from 'lucide-react'
import {
  createContext,
  type ReactNode,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { type OpsAction, type OpsRun, opsActionsApi } from '@/api/opsActions'
import { StatusBadge, TONE_TEXT } from '@/components/ops/StatusBadge'
import { Button } from '@/components/ui/button'
import { recentlyEnded, remedyFor } from '@/lib/workflowRemedies'
import { showToast } from '@/utils/toast'

interface Remedies {
  day: string
  actions: Map<string, OpsAction>
  run: OpsRun | null
  busy: boolean
  start: (action: string) => void
}

const RemedyContext = createContext<Remedies>({
  day: '',
  actions: new Map(),
  run: null,
  busy: false,
  start: () => {},
})

/** Owns the action list and the run being watched for the day on screen. */
export function RemedyProvider({ day, children }: { day: string; children: ReactNode }) {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: ['ops-actions', day],
    queryFn: () => opsActionsApi.list(day),
    enabled: Boolean(day),
    // Watch closely while something runs, otherwise only notice a run started elsewhere.
    refetchInterval: (q) => (q.state.data?.run && !q.state.data.run.outcome ? 3000 : 60_000),
  })
  const run = query.data?.run ?? null
  const running = Boolean(run && !run.outcome)

  const mutation = useMutation({
    mutationFn: (action: string) => opsActionsApi.run(action, day),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['ops-actions'] }),
    onError: (error: Error) => showToast.error(error.message),
  })

  // When a run ends, the day's results changed: reload them once.
  const seen = useRef<string | null>(null)
  useEffect(() => {
    if (run?.outcome && seen.current && seen.current === run.id) {
      for (const key of ['ops-workflow', 'ops-report', 'finding-links', 'ops-actions']) {
        queryClient.invalidateQueries({ queryKey: [key] })
      }
    }
    seen.current = running ? (run?.id ?? null) : null
  }, [run?.id, run?.outcome, running, queryClient])

  const value = useMemo<Remedies>(
    () => ({
      day,
      actions: new Map((query.data?.actions ?? []).map((a) => [a.key, a])),
      run,
      busy: running || mutation.isPending,
      start: (action) => mutation.mutate(action),
    }),
    [day, query.data, run, running, mutation]
  )
  return <RemedyContext.Provider value={value}>{children}</RemedyContext.Provider>
}

/** The button for one finding, or nothing when no script can help with it. */
export function RunAgainButton({
  finding,
}: {
  finding: { phase: string; group: string; name: string }
}) {
  const { actions, busy, start } = useContext(RemedyContext)
  const [confirming, setConfirming] = useState(false)
  const key = remedyFor(finding)
  const action = key ? actions.get(key) : undefined
  if (!action) return null
  const blocked = !action.available || busy
  const why = !action.available ? action.reason : busy ? 'Another run is in progress.' : ''
  return (
    <>
      <button
        type="button"
        disabled={blocked}
        title={why || action.detail}
        onClick={() => setConfirming(true)}
        className={`flex items-center gap-1 text-[13px] font-medium ${
          blocked ? 'cursor-not-allowed text-muted-foreground' : 'text-primary hover:underline'
        }`}
      >
        <Play className="h-3 w-3" />
        Run again
      </button>
      {confirming && (
        <div className="basis-full rounded-md border bg-muted/40 p-3 text-[15px]">
          <div className="font-medium">{action.label}</div>
          <p className="text-[13px] text-muted-foreground">
            {action.detail} This runs the step again. It cannot fix a cause outside the system, for
            example a PC that was asleep, so if it fails again the cause is still there.
          </p>
          <div className="mt-2 flex gap-2">
            <Button
              size="sm"
              disabled={blocked}
              onClick={() => {
                setConfirming(false)
                start(action.key)
              }}
            >
              Run now
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setConfirming(false)}>
              Cancel
            </Button>
          </div>
        </div>
      )}
    </>
  )
}

/** One line about the run in progress, or the one that just finished. */
export function RunBanner() {
  const { run } = useContext(RemedyContext)
  const [dismissed, setDismissed] = useState<string | null>(null)
  if (!run || dismissed === run.id) return null
  const running = !run.outcome
  if (!running && !recentlyEnded(run.ended)) return null
  return (
    <output className="block rounded-lg border px-4 py-3 text-[15px]">
      <div className="flex flex-wrap items-center gap-2">
        {running ? (
          <>
            <Loader2 className="h-4 w-4 animate-spin" />
            <span className="font-medium">Running: {run.label}</span>
            <span className="text-muted-foreground">
              for {run.day}, started {run.auto ? 'automatically' : ''} {run.started.slice(11, 16)}.
              This page updates when it ends.
            </span>
          </>
        ) : (
          <>
            <StatusBadge tone={run.outcome === 'ok' ? 'good' : 'bad'}>
              {run.outcome === 'ok' ? 'Done' : 'Did not finish'}
            </StatusBadge>
            <span className="font-medium">{run.label}</span>
            {run.auto && <span className="text-muted-foreground">(started automatically)</span>}
            <span className={run.outcome === 'ok' ? '' : TONE_TEXT.bad}>{run.message}</span>
            <button
              type="button"
              aria-label="Dismiss"
              className="ml-auto text-muted-foreground hover:text-foreground"
              onClick={() => setDismissed(run.id)}
            >
              <X className="h-4 w-4" />
            </button>
          </>
        )}
      </div>
      {!running && run.outcome === 'failed' && run.tail && (
        <details className="mt-2">
          <summary className="cursor-pointer text-[13px] text-muted-foreground">
            What it printed
          </summary>
          <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted/60 p-2 text-[13px] font-mono">
            {run.tail}
          </pre>
        </details>
      )}
    </output>
  )
}
