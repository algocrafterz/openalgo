import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { BugKind, BugSeverity } from '@/api/bugs'
import { opsFindingsApi } from '@/api/opsFindings'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { AREAS, type Area, buildPayload, defaultsFor } from '@/lib/reportFinding'
import { type Finding, SEVERITY_LABEL } from '@/lib/workflowFindings'
import { showToast } from '@/utils/toast'

const RISK: Record<BugSeverity, string> = {
  HIGH: 'High risk',
  MEDIUM: 'Medium risk',
  LOW: 'Low risk',
}
const KIND: Record<BugKind, string> = {
  BUG: 'Code bug',
  EXECUTION: 'Broker / timing',
  DATA: 'Wrong data',
  'TUNING-IDEA': 'Improvement idea',
}

/** Reports one finding. The server looks the finding up itself; only the choices below are sent. */
export function ReportBugDialog({
  finding,
  day,
  onClose,
}: {
  finding: Finding
  day: string
  onClose: () => void
}) {
  const [form, setForm] = useState(() => defaultsFor(finding))
  const [confirmed, setConfirmed] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const client = useQueryClient()
  const mutation = useMutation({
    mutationFn: () => opsFindingsApi.report(buildPayload(finding, day, form, confirmed)),
    onSuccess: (result) => {
      client.invalidateQueries({ queryKey: ['finding-links'] })
      client.invalidateQueries({ queryKey: ['bug-tracker'] })
      const id = result.bug.id
      showToast.success(
        result.created
          ? `Reported as ${id}`
          : result.recorded
            ? `Already tracked as ${id}. This day was added to it.`
            : `Already tracked as ${id}. Nothing changed.`
      )
      onClose()
    },
    onError: (e: Error) => setError(e.message),
  })

  return (
    <Dialog open onOpenChange={(open) => !open && !mutation.isPending && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Report as bug</DialogTitle>
          <DialogDescription>
            {SEVERITY_LABEL[finding.severity]} finding from the {finding.source.toLowerCase()} on{' '}
            {day}. The recorded evidence is attached automatically. If this finding already has a
            bug, this day is added to it instead of creating a second one.
          </DialogDescription>
        </DialogHeader>

        <p className="rounded-md bg-muted/50 p-3 text-[15px]">{finding.title}</p>

        <div className="grid gap-3 sm:grid-cols-3">
          <div className="space-y-1">
            <Label htmlFor="rb-risk">Risk level</Label>
            <Select
              value={form.severity}
              onValueChange={(v) => {
                setForm({ ...form, severity: v as BugSeverity })
                setConfirmed(false)
              }}
            >
              <SelectTrigger id="rb-risk">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(Object.keys(RISK) as BugSeverity[]).map((k) => (
                  <SelectItem key={k} value={k}>
                    {RISK[k]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1">
            <Label htmlFor="rb-kind">Type</Label>
            <Select
              value={form.kind}
              onValueChange={(v) => setForm({ ...form, kind: v as BugKind })}
            >
              <SelectTrigger id="rb-kind">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(Object.keys(KIND) as BugKind[]).map((k) => (
                  <SelectItem key={k} value={k}>
                    {KIND[k]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1">
            <Label htmlFor="rb-area">Component</Label>
            <Select value={form.area} onValueChange={(v) => setForm({ ...form, area: v as Area })}>
              <SelectTrigger id="rb-area">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {AREAS.map((a) => (
                  <SelectItem key={a} value={a}>
                    {a}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        <div className="flex items-start gap-2 rounded-md border p-3">
          <Checkbox
            id="rb-confirm"
            checked={confirmed}
            onCheckedChange={(v) => setConfirmed(v === true)}
            className="mt-0.5"
          />
          <Label htmlFor="rb-confirm" className="text-[15px] font-normal leading-snug">
            I confirm the risk level is <span className="font-semibold">{RISK[form.severity]}</span>
            . It starts at Medium as a default; it is not taken from this finding.
          </Label>
        </div>

        <div className="space-y-1">
          <Label htmlFor="rb-impact">Trading impact</Label>
          <Textarea
            id="rb-impact"
            rows={3}
            maxLength={600}
            value={form.impact}
            onChange={(e) => setForm({ ...form, impact: e.target.value })}
          />
          <p className="text-[13px] text-muted-foreground">
            It stays "Not assessed" until you describe what this costs. Nothing is inferred from the
            finding.
          </p>
        </div>

        {error && (
          <p role="alert" className="text-[15px] text-red-600 dark:text-red-400">
            {error}
          </p>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={mutation.isPending}>
            Cancel
          </Button>
          <Button
            onClick={() => {
              setError(null)
              mutation.mutate()
            }}
            disabled={mutation.isPending || !confirmed || form.impact.trim() === ''}
          >
            {mutation.isPending ? 'Reporting...' : 'Report bug'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
