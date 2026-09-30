import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

/**
 * The idea.md body: experiments, rejected variants, observations and the decision log.
 * Shown as plain preformatted text, after and apart from the strategy definition.
 */
export function ResearchNotes({ notes }: { notes: string | undefined }) {
  if (!notes) return null
  return (
    <Card data-testid="research-notes">
      <CardHeader>
        <CardTitle>Research notes</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <p className="text-xs text-muted-foreground">
          History and experiments. This is not the current strategy definition.
        </p>
        <pre className="whitespace-pre-wrap text-sm font-sans">{notes}</pre>
      </CardContent>
    </Card>
  )
}
