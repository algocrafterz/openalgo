import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

/**
 * The idea.md body: experiments, rejected variants, observations and the decision log.
 * Collapsed by default and shown as plain text: it is history, not the current strategy
 * definition, and is rarely what a reader came for.
 */
export function ResearchNotes({ notes }: { notes: string | undefined }) {
  if (!notes) return null
  return (
    <Card data-testid="research-notes">
      <CardHeader>
        <CardTitle>Research notes</CardTitle>
      </CardHeader>
      <CardContent>
        <details>
          <summary className="cursor-pointer text-sm underline underline-offset-4">
            Show history and experiments
          </summary>
          <p className="text-xs text-muted-foreground mt-2">
            History and experiments. This is not the current strategy definition.
          </p>
          <pre className="whitespace-pre-wrap break-words text-sm font-sans mt-2">{notes}</pre>
        </details>
      </CardContent>
    </Card>
  )
}
