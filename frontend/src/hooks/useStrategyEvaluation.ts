import { useMutation } from '@tanstack/react-query'
import { useCallback, useRef, useState } from 'react'
import {
  type DateRange,
  type EvaluationChecks,
  type EvaluationPackage,
  type EvaluationProblem,
  type ImportedEvaluation,
  type PerformancePeriod,
  strategyPnlApi,
} from '@/api/strategyPnl'
import type { CapitalMode } from '@/lib/capital'
import { selectionSignature } from '@/lib/evaluationPackage'
import type { StrategyFilter } from '@/lib/strategyFilter'

export const PREPARE_FAILED = 'The evaluation could not be prepared. Try again in a moment.'
export const VALIDATE_FAILED = 'The analysis could not be checked. Try again in a moment.'

export interface EvaluationSelection {
  filter: StrategyFilter
  period: PerformancePeriod
  range: DateRange | undefined
  capital: number | null
  mode: CapitalMode
}

export interface ValidationFailure {
  message: string
  problems: EvaluationProblem[]
}

export interface ImportedAnalysis {
  evaluation: ImportedEvaluation
  checks: EvaluationChecks
  /** The selection the check was run against. */
  signature: string
}

function errorText(error: unknown, fallback: string): string {
  // The page guards "nothing selected" itself; its message is already plain text.
  return error instanceof Error && error.message ? error.message : fallback
}

/**
 * The evaluation package and the analysis pasted back, for one Performance
 * selection. Both calls run only when the trader presses a button: there is no
 * polling and nothing is stored in the browser or on the server, so a reload
 * clears everything.
 */
export function useStrategyEvaluation(selection: EvaluationSelection) {
  const { filter, period, range, capital, mode } = selection
  const currentSignature = selectionSignature(filter, period, range, capital, mode)

  const [pkg, setPkg] = useState<EvaluationPackage | null>(null)
  // Counts successful prepares. A late check for an older package is told apart by it.
  const [packageKey, setPackageKey] = useState(0)
  const packageKeyRef = useRef(0)
  const [preparedSignature, setPreparedSignature] = useState<string | null>(null)
  const [prepareError, setPrepareError] = useState<string | null>(null)
  const [imported, setImported] = useState<ImportedAnalysis | null>(null)
  const [failure, setFailure] = useState<ValidationFailure | null>(null)
  // A press that lands before React has re-rendered the pending state is still ignored.
  const preparing = useRef(false)
  const validating = useRef(false)

  const prepareMutation = useMutation({
    mutationFn: () => strategyPnlApi.prepareEvaluation(filter, period, range, capital),
  })
  const validateMutation = useMutation({
    mutationFn: (response: string) =>
      strategyPnlApi.validateEvaluation(filter, period, range, capital, response),
  })

  const { mutateAsync: runPrepare } = prepareMutation
  const { mutateAsync: runValidate } = validateMutation

  const prepare = useCallback(async () => {
    if (preparing.current) return
    preparing.current = true
    const signature = currentSignature
    setPrepareError(null)
    try {
      const res = await runPrepare()
      if (res.status === 'success' && res.package) {
        packageKeyRef.current += 1
        setPackageKey(packageKeyRef.current)
        setPkg(res.package)
        setPreparedSignature(signature)
        // A new package is a new question: an analysis pasted for the old one is dropped.
        setImported(null)
        setFailure(null)
      } else {
        setPrepareError(res.message || PREPARE_FAILED)
      }
    } catch (error) {
      setPrepareError(errorText(error, PREPARE_FAILED))
    } finally {
      preparing.current = false
    }
  }, [runPrepare, currentSignature])

  const validate = useCallback(
    async (response: string) => {
      if (validating.current) return
      validating.current = true
      const requestKey = packageKeyRef.current
      const signature = currentSignature
      try {
        const res = await runValidate(response)
        // The answer belongs to an older package: drop it instead of attaching it to the new one.
        if (requestKey !== packageKeyRef.current) return
        if (res.status === 'success' && res.evaluation && res.checks) {
          setImported({ evaluation: res.evaluation, checks: res.checks, signature })
          setFailure(null)
        } else {
          setImported(null)
          setFailure({
            message: res.message || VALIDATE_FAILED,
            problems: Array.isArray(res.problems) ? res.problems : [],
          })
        }
      } catch (error) {
        if (requestKey !== packageKeyRef.current) return
        setImported(null)
        setFailure({ message: errorText(error, VALIDATE_FAILED), problems: [] })
      } finally {
        validating.current = false
      }
    },
    [runValidate, currentSignature]
  )

  return {
    pkg,
    packageKey,
    prepare,
    preparing: prepareMutation.isPending,
    prepareError,
    validate,
    validating: validateMutation.isPending,
    imported,
    failure,
    /** True when the shown analysis was checked against an earlier selection. */
    importedEarlier: imported !== null && imported.signature !== currentSignature,
    /** True when the selection changed after the package was prepared. */
    stale: pkg !== null && preparedSignature !== currentSignature,
  }
}
