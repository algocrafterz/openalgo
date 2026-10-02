/** First eight characters of a hash with the full value on hover; tolerates a missing value. */
export function ShortHash({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="text-muted-foreground italic">not captured</span>
  return (
    <span title={value} className="font-mono">
      {value.slice(0, 8)}
    </span>
  )
}
