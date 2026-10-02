import type { RunFingerprints, RunReproducibility } from '@/api/backtestResults'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { InfoTip } from './InfoTip'
import { ShortHash } from './ShortHash'

const CHECKLIST_LABELS: Record<string, string> = {
  strategy_version: 'Strategy version',
  strategy_fingerprint: 'Strategy fingerprint',
  dataset_fingerprint: 'Dataset fingerprint',
  execution_config: 'Execution configuration',
  deployment_profile: 'Deployment profile',
  engine_version: 'Engine version',
  code_commit: 'Code commit',
  resolved_assumptions: 'Resolved assumptions',
}

const FINGERPRINT_LABELS: [keyof RunFingerprints, string][] = [
  ['rules', 'Rules'],
  ['params', 'Parameters'],
  ['strategy', 'Strategy'],
  ['execution', 'Execution'],
  ['cost', 'Cost'],
  ['universe', 'Universe'],
  ['profile', 'Profile'],
  ['dataset', 'Dataset'],
]

function label(key: string): string {
  return CHECKLIST_LABELS[key] ?? key
}

/** Whether this run can be rebuilt exactly, what was captured, and the last attempt to do so. */
export function Reproducibility({
  reproducibility,
  fingerprints,
  manifestSha256,
  changeClasses,
}: {
  reproducibility: RunReproducibility | null
  fingerprints: RunFingerprints | null
  manifestSha256: string | null
  changeClasses?: string[]
}) {
  if (reproducibility === null) {
    return (
      <Card data-testid="reproducibility">
        <CardHeader>
          <CardTitle>Reproducibility</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">Not captured for this run</p>
        </CardContent>
      </Card>
    )
  }
  const r = reproducibility
  const checklist = Object.entries(r.checklist ?? {})
  const unchecked = checklist.filter(([, ok]) => !ok).map(([k]) => k)
  const attempt = r.last_attempt
  return (
    <Card data-testid="reproducibility">
      <CardHeader>
        <CardTitle>Reproducibility</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        <div data-testid="repro-state" className="space-y-1">
          <div className="font-medium">
            Reproducible: {r.reproducible ? 'Yes' : 'No'}
            <InfoTip term="reproducible_run" />
          </div>
          {!r.reproducible && (
            <ul className="list-disc pl-5 text-muted-foreground">
              {r.code_dirty && (
                <li>
                  Uncommitted changes in the working tree when the run was made (dirty):
                  <ul className="list-none pl-3 font-mono">
                    {(r.dirty_paths ?? []).map((p) => (
                      <li key={p}>{p}</li>
                    ))}
                  </ul>
                </li>
              )}
              {unchecked.map((k) => (
                <li key={k}>{label(k)} was not captured</li>
              ))}
            </ul>
          )}
        </div>
        {!r.reproducible && (
          <p data-testid="repro-not-eligible" className="font-medium">
            Not eligible for promotion until the run is reproducible.
          </p>
        )}
        <div>
          <div className="font-medium mb-1">Checklist</div>
          <ul className="space-y-0.5">
            {checklist.map(([k, ok]) => (
              <li key={k} data-testid={`repro-check-${k}`} className="flex gap-3">
                <span className="w-56">{label(k)}</span>
                <span className={ok ? '' : 'font-medium'}>{ok ? 'Captured' : 'Not captured'}</span>
              </li>
            ))}
          </ul>
        </div>
        <div data-testid="repro-engine">
          Engine version {r.engine_version ?? 'not captured'}, commit{' '}
          <ShortHash value={r.code_commit} />
          {r.code_dirty ? ' (dirty)' : ''}
        </div>
        {changeClasses && changeClasses.length > 0 && (
          <div data-testid="repro-change-classes">Change classes: {changeClasses.join(', ')}</div>
        )}
        <div>
          <div className="font-medium mb-1">Fingerprints</div>
          {fingerprints ? (
            <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-6 gap-y-0.5">
              {FINGERPRINT_LABELS.map(([k, name]) => (
                <div key={k} className="contents">
                  <dt className="text-muted-foreground">{name}</dt>
                  <dd>
                    <ShortHash value={fingerprints[k]} />
                  </dd>
                </div>
              ))}
              {manifestSha256 && (
                <div className="contents">
                  <dt className="text-muted-foreground">Manifest</dt>
                  <dd>
                    <ShortHash value={manifestSha256} />
                  </dd>
                </div>
              )}
            </dl>
          ) : (
            <span className="text-muted-foreground">not captured</span>
          )}
        </div>
        <div data-testid="repro-last-attempt">
          <div className="font-medium mb-1">Last reproduction attempt</div>
          {attempt ? (
            <div>
              {attempt.attempted_ist}: {attempt.reproducible ? 'Passed' : 'Failed'}
              {(attempt.reasons ?? []).length > 0 && (
                <ul className="list-disc pl-5 text-muted-foreground">
                  {(attempt.reasons ?? []).map((x) => (
                    <li key={x}>{x}</li>
                  ))}
                </ul>
              )}
            </div>
          ) : (
            <span className="text-muted-foreground">Never reproduced</span>
          )}
        </div>
      </CardContent>
    </Card>
  )
}
