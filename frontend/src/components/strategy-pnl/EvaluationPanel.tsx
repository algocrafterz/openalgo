import { Loader2 } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import type { DateRange, PerformancePeriod } from '@/api/strategyPnl'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Textarea } from '@/components/ui/textarea'
import { useStrategyEvaluation } from '@/hooks/useStrategyEvaluation'
import type { CapitalMode } from '@/lib/capital'
import { copyToClipboard } from '@/lib/clipboard'
import {
  downloadFilename,
  MAX_ANALYSIS_CHARS,
  problemsToLines,
  promptForUse,
} from '@/lib/evaluationPackage'
import type { StrategyFilter } from '@/lib/strategyFilter'
import { ClaudeAnalysisView } from './ClaudeAnalysisView'
import { EvaluationReportView } from './EvaluationReportView'

export interface EvaluationPanelProps {
  filter: StrategyFilter
  period: PerformancePeriod
  range: DateRange | undefined
  capital: number | null
  clientMode: CapitalMode
  nothingSelected: boolean
  tradesCount: number
  /** False while the page is still settling its capital and queries. */
  ready: boolean
}

export const DISCLOSURE =
  'When you submit this to Claude yourself, this data leaves your OpenAlgo server and goes to Anthropic under your own account. OpenAlgo does not send it and does not check what Claude does with it.'

const STEPS =
  "1. Copy the prompt. 2. Download the data file. 3. Open claude.ai, attach the file and paste the prompt. 4. Paste Claude's answer back below."

/** Why the prepare button is off, or null when it can be pressed. */
export function disabledReason(props: {
  nothingSelected: boolean
  ready: boolean
  tradesCount: number
}): string | null {
  if (props.nothingSelected) return 'Select at least one strategy.'
  if (!props.ready) return 'Waiting for the page data'
  if (props.tradesCount <= 0) return 'There are no closed trades in this selection to evaluate.'
  return null
}

type EvaluationPackageValue = NonNullable<ReturnType<typeof useStrategyEvaluation>['pkg']>

/**
 * The copy and download block for one package. The panel keys it on the package,
 * so a new package starts with a clean copy note without any reset effect.
 */
function PromptActions({ pkg, stale }: { pkg: EvaluationPackageValue; stale: boolean }) {
  const [withData, setWithData] = useState(true)
  const [copyNote, setCopyNote] = useState<string | null>(null)
  const [fallbackText, setFallbackText] = useState<string | null>(null)
  const [downloadError, setDownloadError] = useState<string | null>(null)
  const fallbackRef = useRef<HTMLTextAreaElement>(null)

  // The text is selected as soon as the fallback box appears, so Ctrl+C is all that is left.
  useEffect(() => {
    if (fallbackText !== null) fallbackRef.current?.select()
  }, [fallbackText])

  const choice = promptForUse(pkg, withData)

  const changeWithData = (value: boolean) => {
    setWithData(value)
    // The note described the other prompt.
    setCopyNote(null)
    setFallbackText(null)
  }

  const copyPrompt = async () => {
    const ok = await copyToClipboard(choice.text)
    if (ok) {
      setFallbackText(null)
      setCopyNote(
        choice.includesData
          ? 'Prompt copied, with the data inside it.'
          : `Prompt copied. ${choice.note ?? ''}`.trim()
      )
    } else {
      setFallbackText(choice.text)
      setCopyNote(
        'Your browser blocked copying. The prompt is selected below. Press Ctrl+C to copy it.'
      )
    }
  }

  const download = () => {
    try {
      const blob = new Blob([pkg.export.data_json], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = downloadFilename(pkg)
      document.body.appendChild(link)
      link.click()
      link.remove()
      URL.revokeObjectURL(url)
      setDownloadError(null)
    } catch {
      setDownloadError('The data file could not be saved. Try again.')
    }
  }

  return (
    <section aria-label="Deeper analysis with Claude" className="space-y-3">
      <h3 className="text-sm font-semibold">Deeper analysis with Claude (manual)</h3>
      <div className="space-y-1 rounded-md border bg-muted p-3 text-sm">
        <p className="whitespace-pre-wrap break-words">{pkg.export.notice}</p>
        <p>{DISCLOSURE}</p>
      </div>
      <p className="text-sm">{STEPS}</p>
      {stale && (
        <p className="text-xs text-muted-foreground">
          Stale: this prompt and file belong to the earlier selection.
        </p>
      )}
      <div className="flex items-center gap-2">
        <Checkbox
          id="evaluation-include-data"
          checked={withData}
          onCheckedChange={(value) => changeWithData(value === true)}
        />
        <label htmlFor="evaluation-include-data" className="text-sm">
          Include the data in the prompt
        </label>
      </div>
      <div className="flex flex-wrap gap-2">
        <Button variant="outline" onClick={() => void copyPrompt()}>
          {stale ? 'Copy prompt (stale)' : 'Copy prompt'}
        </Button>
        <Button variant="outline" onClick={download}>
          {stale ? 'Download evaluation data (stale)' : 'Download evaluation data'}
        </Button>
      </div>
      {(!choice.includesData || choice.note) && (
        <p className="text-xs text-muted-foreground">{choice.note}</p>
      )}
      {copyNote && <output className="block text-sm">{copyNote}</output>}
      {fallbackText !== null && (
        <Textarea
          ref={fallbackRef}
          readOnly
          aria-label="Prompt to copy"
          value={fallbackText}
          rows={8}
          className="font-mono text-xs"
          onFocus={(e) => e.currentTarget.select()}
        />
      )}
      {downloadError && (
        <p role="alert" className="text-sm">
          {downloadError}
        </p>
      )}
    </section>
  )
}

export function EvaluationPanel(props: EvaluationPanelProps) {
  const { filter, period, range, capital, clientMode } = props
  const evaluation = useStrategyEvaluation({ filter, period, range, capital, mode: clientMode })
  const { pkg, stale, preparing, validating, imported, failure } = evaluation
  const reason = disabledReason(props)
  const [pasted, setPasted] = useState('')

  const overLimit = pasted.length > MAX_ANALYSIS_CHARS

  return (
    <Card>
      <CardHeader>
        <CardTitle>AI evaluation</CardTitle>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-3">
            <Button
              onClick={() => void evaluation.prepare()}
              disabled={reason !== null || preparing}
            >
              {preparing && <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />}
              {preparing ? 'Preparing' : pkg ? 'Prepare again' : 'Prepare AI Evaluation'}
            </Button>
            {reason && <span className="text-sm text-muted-foreground">{reason}</span>}
          </div>
          {preparing && (
            <output className="block text-sm text-muted-foreground">
              Preparing the evaluation from your closed trades.
            </output>
          )}
          {evaluation.prepareError && (
            <div role="alert" className="rounded-md border bg-muted p-3 text-sm break-words">
              {evaluation.prepareError}
            </div>
          )}
          <p className="text-xs text-muted-foreground">
            Nothing is saved. Reloading the page clears the evaluation and any analysis you pasted.
          </p>
        </div>

        {pkg && stale && (
          <div
            role="alert"
            className="rounded-md border border-amber-500/50 bg-amber-500/10 p-3 text-sm"
          >
            The selection changed after this was prepared. Prepare it again before using it.
          </div>
        )}

        {pkg && <EvaluationReportView pkg={pkg} />}

        {pkg && <PromptActions key={evaluation.packageKey} pkg={pkg} stale={stale} />}

        {pkg && (
          <section aria-label="Claude's analysis pasted by you" className="space-y-3">
            <h3 className="text-sm font-semibold">Claude's analysis (pasted by you)</h3>
            <Textarea
              aria-label="Claude's analysis"
              value={pasted}
              rows={8}
              placeholder="Paste Claude's answer here"
              className="font-mono text-xs"
              onChange={(e) => setPasted(e.target.value)}
            />
            <div className="flex flex-wrap items-center gap-3">
              <Button
                variant="outline"
                onClick={() => void evaluation.validate(pasted)}
                disabled={validating || pasted.trim() === '' || overLimit || reason !== null}
              >
                {validating && <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />}
                {validating ? 'Checking' : 'Check and show'}
              </Button>
              <span className="text-xs text-muted-foreground">
                {pasted.length.toLocaleString('en-IN')} of{' '}
                {MAX_ANALYSIS_CHARS.toLocaleString('en-IN')} characters
              </span>
            </div>
            {overLimit && (
              <p role="alert" className="text-sm">
                That is longer than 60,000 characters. Paste only Claude's answer.
              </p>
            )}
            {failure && (
              <div role="alert" className="space-y-1 rounded-md border bg-muted p-3 text-sm">
                <p className="whitespace-pre-wrap break-words">{failure.message}</p>
                {problemsToLines(failure.problems).length > 0 && (
                  <ul className="list-disc space-y-1 pl-5">
                    {problemsToLines(failure.problems).map((line, index) => (
                      <li key={`${index}-${line}`} className="whitespace-pre-wrap break-words">
                        {line}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
            {imported && evaluation.importedEarlier && (
              <p className="text-xs text-muted-foreground">Checked against an earlier selection.</p>
            )}
            {imported && (
              <ClaudeAnalysisView evaluation={imported.evaluation} checks={imported.checks} />
            )}
          </section>
        )}
      </CardContent>
    </Card>
  )
}
