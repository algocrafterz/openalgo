import type {
  AnalysisObservation,
  AnalysisPortfolio,
  AnalysisStrategy,
  EvaluationChecks,
  ImportedEvaluation,
} from '@/api/strategyPnl'
import { Badge } from '@/components/ui/badge'
import { observationKindText } from '@/lib/evaluationPackage'
import { EvidenceBadge } from './EvaluationReportView'

export const PACKAGE_MISMATCH_TEXT =
  'This analysis was written for different data than the current selection.'
const CONFLICT_TEXT = 'Differs from the calculated label'

function TextList({ title, items }: { title: string; items: string[] }) {
  if (items.length === 0) return null
  return (
    <div>
      <h5 className="text-xs font-medium">{title}</h5>
      <ul className="list-disc space-y-1 pl-5 text-sm">
        {items.map((text, index) => (
          <li key={index} className="whitespace-pre-wrap break-words">
            {text}
          </li>
        ))}
      </ul>
    </div>
  )
}

function Observations({ items }: { items: AnalysisObservation[] }) {
  if (items.length === 0) return null
  return (
    <div>
      <h5 className="text-xs font-medium">Observations</h5>
      <ul className="space-y-2">
        {items.map((item, index) => (
          <li key={index} className="rounded-md border p-2">
            <div className="flex flex-wrap items-start gap-2">
              <Badge variant="outline">{observationKindText(item.kind)}</Badge>
              <p className="min-w-0 flex-1 whitespace-pre-wrap break-words text-sm">{item.text}</p>
            </div>
            <p className="break-words text-xs text-muted-foreground">
              {item.n === null ? 'No trade count given' : `Based on ${item.n} trades`}
              {item.evidence.length > 0 ? `. Data used: ${item.evidence.join(', ')}` : ''}
            </p>
          </li>
        ))}
      </ul>
    </div>
  )
}

function LabelPair({
  claude,
  calculated,
  conflict,
}: {
  claude: string | null
  calculated: string | null
  conflict: boolean
}) {
  return (
    <div className="grid gap-2 text-sm sm:grid-cols-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted-foreground">Claude's assessment</span>
        <EvidenceBadge label={claude} />
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted-foreground">Calculated by OpenAlgo</span>
        <EvidenceBadge label={calculated} />
      </div>
      {conflict && (
        <Badge variant="outline" className="w-fit whitespace-normal sm:col-span-2">
          {CONFLICT_TEXT}
        </Badge>
      )}
    </div>
  )
}

function StrategyBlock({ item }: { item: AnalysisStrategy }) {
  return (
    <div className="space-y-2 rounded-md border p-3">
      <h4 className="break-words text-sm font-semibold">{item.name ?? 'Unnamed strategy'}</h4>
      <LabelPair
        claude={item.assessment}
        calculated={item.deterministic_label}
        conflict={item.conflict}
      />
      <p className="text-xs text-muted-foreground">
        Claude counted {item.n ?? 'an unknown number of'} trades. Claude's confidence:{' '}
        {item.confidence ?? 'not given'}.
      </p>
      <Observations items={item.observations} />
      <TextList title="Weaknesses" items={item.weaknesses} />
      <TextList title="Ideas to test, not instructions" items={item.suggested_tests} />
    </div>
  )
}

function PortfolioBlock({ item }: { item: AnalysisPortfolio }) {
  return (
    <div className="space-y-2 rounded-md border p-3">
      <h4 className="text-sm font-semibold">All selected strategies together</h4>
      <LabelPair
        claude={item.assessment}
        calculated={item.deterministic_label}
        conflict={item.conflict}
      />
      <Observations items={item.observations} />
      <TextList title="Overlap between strategies" items={item.overlap_notes} />
    </div>
  )
}

/** Notes shown above the analysis: different data, label conflicts, and the server's warnings. */
export function AnalysisChecks({ checks }: { checks: EvaluationChecks }) {
  const warnings = checks.warnings.filter((text) => text !== PACKAGE_MISMATCH_TEXT)
  if (checks.package_match && checks.label_conflicts.length === 0 && warnings.length === 0) {
    return null
  }
  return (
    <output className="block space-y-1 rounded-md border bg-muted p-3 text-sm">
      {!checks.package_match && <p>{PACKAGE_MISMATCH_TEXT}</p>}
      {checks.label_conflicts.map((conflict, index) => (
        <p key={index} className="whitespace-pre-wrap break-words">
          {conflict.strategy ? `${conflict.strategy}: ` : ''}
          {conflict.message}
        </p>
      ))}
      {warnings.map((text, index) => (
        <p key={index} className="whitespace-pre-wrap break-words">
          {text}
        </p>
      ))}
    </output>
  )
}

/** The analysis pasted by the trader, shown apart from the figures OpenAlgo calculated. */
export function ClaudeAnalysisView({
  evaluation,
  checks,
}: {
  evaluation: ImportedEvaluation
  checks: EvaluationChecks
}) {
  return (
    <section
      aria-label="Claude's analysis"
      className="space-y-3 rounded-md border-2 border-dashed p-3"
    >
      <h3 className="text-sm font-semibold">Claude's analysis - not calculated by OpenAlgo</h3>
      <AnalysisChecks checks={checks} />
      {evaluation.summary && (
        <p className="whitespace-pre-wrap break-words text-sm">{evaluation.summary}</p>
      )}
      <p className="text-xs text-muted-foreground">
        Claude's overall confidence: {evaluation.overall_confidence ?? 'not given'}
      </p>
      {evaluation.portfolio && <PortfolioBlock item={evaluation.portfolio} />}
      {evaluation.strategies.map((item, index) => (
        <StrategyBlock key={`${item.name}-${index}`} item={item} />
      ))}
      <TextList title="Limitations Claude gave" items={evaluation.limitations} />
    </section>
  )
}
