import { useState } from 'react'
import type {
  EvaluationFinding,
  EvaluationPackage,
  EvaluationReport,
  EvaluationSection,
  EvidenceLabel,
} from '@/api/strategyPnl'
import { Badge } from '@/components/ui/badge'
import { evidenceLabelClass, findingKindBadge, ruleParameterParts } from '@/lib/evaluationPackage'
import { cn } from '@/lib/utils'

/** Sections that start open. The rest start closed so the report stays short. */
const OPEN_SECTIONS = new Set(['sample_size', 'weaknesses'])

export function EvidenceBadge({ label }: { label: EvidenceLabel | string | null | undefined }) {
  return (
    <Badge variant="outline" className={cn('whitespace-normal', evidenceLabelClass(label))}>
      {label ?? 'not available'}
    </Badge>
  )
}

function FindingRow({
  finding,
  legend,
}: {
  finding: EvaluationFinding
  legend: string | undefined
}) {
  const badge = findingKindBadge(finding.kind)
  const parts = ruleParameterParts(finding.rule?.parameters)
  return (
    <li className="space-y-1 rounded-md border p-3">
      <div className="flex flex-wrap items-start gap-2">
        <Badge variant="outline" className={badge.className}>
          {badge.text}
        </Badge>
        <p className="min-w-0 flex-1 whitespace-pre-wrap break-words text-sm">
          {finding.statement}
        </p>
      </div>
      <p className="text-xs text-muted-foreground">
        {finding.scope !== 'pooled' && <span className="break-words">{finding.scope}. </span>}
        Based on {finding.n} {finding.n === 1 ? 'trade' : 'trades'}.
      </p>
      {finding.kind === 'heuristic' && (
        <p className="whitespace-pre-wrap break-words text-xs text-muted-foreground">
          {finding.rule ? `Rule: ${finding.rule.name}` : 'Rule of thumb'}
          {parts.length > 0 ? ` (${parts.join(', ')})` : ''}.{' '}
          {legend ?? 'The cut-off is a choice, not a statistical proof.'}
        </p>
      )}
      {finding.note && (
        <p className="whitespace-pre-wrap break-words text-xs text-muted-foreground">
          {finding.note}
        </p>
      )}
    </li>
  )
}

function SectionBlock({
  section,
  legend,
}: {
  section: EvaluationSection
  legend: EvaluationReport['legend']
}) {
  const [open, setOpen] = useState(OPEN_SECTIONS.has(section.id))
  return (
    <details
      open={open}
      onToggle={(e) => setOpen(e.currentTarget.open)}
      className="rounded-md border"
    >
      <summary className="cursor-pointer select-none px-3 py-2 text-sm font-medium">
        {section.title}
      </summary>
      <ul className="space-y-2 px-3 pb-3">
        {section.findings.map((finding) => (
          <FindingRow key={finding.id} finding={finding} legend={legend?.[finding.kind]} />
        ))}
      </ul>
    </details>
  )
}

function scopeLine(meta: EvaluationPackage['meta'], report: EvaluationReport): string {
  const { scope } = report
  const days = scope.trading_days === null ? null : `${scope.trading_days} trading days`
  const bits = [
    `${scope.n_trades} closed ${scope.n_trades === 1 ? 'trade' : 'trades'}`,
    days,
    `${scope.strategies} ${scope.strategies === 1 ? 'strategy' : 'strategies'}`,
    `${meta.mode_display} mode`,
    `${meta.start_date} to ${meta.end_date}`,
    meta.capital !== null && meta.capital !== undefined
      ? `capital ${meta.capital.toLocaleString('en-IN')}`
      : null,
  ]
  return bits.filter(Boolean).join(', ')
}

/** The report the server built from the closed trades. Every string is shown as plain text. */
export function EvaluationReportView({ pkg }: { pkg: EvaluationPackage }) {
  const { report, meta } = pkg
  const perStrategy = Object.entries(report.scope.labels.per_strategy)
  return (
    <section aria-label="Quick evaluation" className="space-y-3">
      <div>
        <h3 className="text-sm font-semibold">Quick evaluation (calculated here)</h3>
        <p className="text-xs text-muted-foreground">
          Built from your closed trades in this selection. Nothing leaves your machine at this step.
        </p>
      </div>
      <p className="break-words text-sm">{scopeLine(meta, report)}</p>
      {meta.truncated && (
        <p className="text-xs text-muted-foreground">
          The data was shortened to keep the file a manageable size.
        </p>
      )}
      <div className="space-y-1">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="font-medium">All selected strategies together</span>
          <EvidenceBadge label={report.scope.labels.pooled} />
        </div>
        {perStrategy.map(([name, label], index) => (
          <div key={`${index}-${name}`} className="flex flex-wrap items-center gap-2 text-sm">
            <span className="min-w-0 break-words">{name}</span>
            <EvidenceBadge label={label} />
          </div>
        ))}
      </div>
      <dl className="space-y-1 text-xs text-muted-foreground">
        {Object.entries(report.legend ?? {}).map(([kind, text]) => (
          <div key={kind} className="flex flex-wrap items-start gap-2">
            <dt>
              <Badge variant="outline" className={findingKindBadge(kind).className}>
                {findingKindBadge(kind).text}
              </Badge>
            </dt>
            <dd className="min-w-0 flex-1 break-words">{text}</dd>
          </div>
        ))}
      </dl>
      <div className="space-y-2">
        {report.sections.map((section) => (
          <SectionBlock key={section.id} section={section} legend={report.legend} />
        ))}
      </div>
      <div>
        <h4 className="text-sm font-medium">Limitations</h4>
        <ul className="list-disc space-y-1 pl-5 text-xs text-muted-foreground">
          {report.limitations.map((text, index) => (
            <li key={`${index}-${text}`} className="whitespace-pre-wrap break-words">
              {text}
            </li>
          ))}
        </ul>
      </div>
    </section>
  )
}
