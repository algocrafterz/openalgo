import { Card, CardContent } from '@/components/ui/card'
import { cn } from '@/lib/utils'

export function MetricCard({
  label,
  value,
  positive,
  negative,
  hint,
}: {
  label: string
  value: string
  positive?: boolean
  negative?: boolean
  hint?: string
}) {
  return (
    <Card>
      <CardContent className="py-4">
        <div className="text-xs text-muted-foreground">{label}</div>
        <div
          className={cn(
            'text-xl font-bold font-mono mt-1',
            positive && 'text-green-600',
            negative && 'text-red-600'
          )}
        >
          {value}
        </div>
        {hint && <div className="text-[11px] text-muted-foreground mt-0.5">{hint}</div>}
      </CardContent>
    </Card>
  )
}
