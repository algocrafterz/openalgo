import type { ValidityBlock } from '@/api/backtestResults'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { InfoTip } from './InfoTip'

const CHECKS: [string, string][] = [
  ['data_quality', 'Data quality'],
  ['config', 'Configuration'],
  ['lineage', 'Lineage reconciliation'],
  ['truncation', 'Look-ahead (truncation)'],
]

/** A check that did not run is dashed and muted: absent evidence must never look like a pass. */
const TONE = {
  passed: { text: 'Passed', cls: 'text-green-600 border-green-600/50' },
  failed: { text: 'Failed', cls: 'text-red-600 border-red-600/60 font-medium' },
  skipped: {
    text: 'Did not run',
    cls: 'text-muted-foreground border-dashed border-muted-foreground/50 italic',
  },
}

/** Which integrity checks ran, passed or failed. Read from the record, never recomputed. */
export function IntegrityChecks({ validity }: { validity: ValidityBlock | null }) {
  const failures = validity?.failures ?? []
  return (
    <Card data-testid="integrity-checks" className="max-w-3xl">
      <CardHeader>
        <CardTitle className="text-base">
          Integrity checks <InfoTip term="validity_checks" />
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {!validity ? (
          <p className="text-sm text-muted-foreground">
            Validity checks were not recorded for this run
          </p>
        ) : (
          <>
            {validity.ok === false && (
              <p data-testid="validity-overall" className="text-sm font-medium text-red-600">
                Not ok: at least one integrity check failed.
              </p>
            )}
            <ul className="space-y-1">
              {CHECKS.map(([key, label]) => {
                const failure = failures.find((f) => f.check === key)
                const ran = (validity.checks_run ?? []).includes(key)
                const tone = failure ? TONE.failed : ran ? TONE.passed : TONE.skipped
                return (
                  <li key={key} data-testid={`check-${key}`} className="text-sm">
                    <span className="inline-block w-48">{label}</span>
                    <span className={`rounded border px-1.5 py-0.5 text-xs ${tone.cls}`}>
                      {tone.text}
                    </span>
                    {failure && (
                      <span className="ml-2 text-muted-foreground">{failure.detail}</span>
                    )}
                  </li>
                )
              })}
              {failures
                .filter((f) => !CHECKS.some(([k]) => k === f.check))
                .map((f, i) => (
                  <li key={`${f.check}-${i}`} data-testid={`check-${f.check}`} className="text-sm">
                    <span className="inline-block w-48">{f.check}</span>
                    <span className={`rounded border px-1.5 py-0.5 text-xs ${TONE.failed.cls}`}>
                      {TONE.failed.text}
                    </span>
                    <span className="ml-2 text-muted-foreground">{f.detail}</span>
                  </li>
                ))}
            </ul>
            <p data-testid="lookahead-note" className="text-xs text-muted-foreground">
              The look-ahead check covers single-symbol, frame-derived inputs only. It does not
              cover regime or cross-sectional inputs. <InfoTip term="lookahead_check" />
            </p>
          </>
        )}
      </CardContent>
    </Card>
  )
}
