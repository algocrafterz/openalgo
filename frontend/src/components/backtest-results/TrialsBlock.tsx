import type { TrialsBlock as Trials } from '@/api/backtestResults'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { InfoTip } from './InfoTip'

/** The trial count behind the run, as the ledger recorded it. */
export function TrialsBlock({ trials }: { trials: Trials | null }) {
  return (
    <Card data-testid="trials-block" className="max-w-3xl">
      <CardHeader>
        <CardTitle className="text-base">
          Trials <InfoTip term="trial_ledger" />
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {!trials ? (
          <p className="text-sm text-muted-foreground">Not recorded</p>
        ) : (
          <>
            <dl className="flex flex-wrap gap-6 text-sm">
              <div>
                <dt className="text-muted-foreground">Material</dt>
                <dd className="font-mono" data-testid="trials-material">
                  {trials.material}
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">External</dt>
                <dd className="font-mono" data-testid="trials-external">
                  {trials.external}
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">Total</dt>
                <dd className="font-mono" data-testid="trials-total">
                  {trials.total}
                </dd>
              </div>
            </dl>
            <p className="text-xs text-muted-foreground">Source: {trials.source}</p>
            <p data-testid="trials-explain" className="text-xs text-muted-foreground">
              A trial is any variant of this idea that was evaluated, whether it changed the result
              or came from outside.
            </p>
          </>
        )}
      </CardContent>
    </Card>
  )
}
