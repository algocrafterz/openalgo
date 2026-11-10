import type { ReactNode } from 'react'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'

/**
 * A side view that holds the detail behind one report link (assumptions, rules, validation,
 * history, notes or the trade list). Its content is built only while it is open, so the report
 * itself stays short and light.
 */
export function ReportSheet({
  title,
  description,
  open,
  onOpenChange,
  testId,
  children,
}: {
  title: string
  description: string
  open: boolean
  onOpenChange: (open: boolean) => void
  testId: string
  children: ReactNode
}) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="w-full overflow-y-auto sm:max-w-4xl"
        data-testid={testId}
        // Do not jump to the first help icon, which would open its tip as the view appears.
        onOpenAutoFocus={(e) => e.preventDefault()}
      >
        <SheetHeader>
          <SheetTitle>{title}</SheetTitle>
          <SheetDescription>{description}</SheetDescription>
        </SheetHeader>
        <div className="space-y-6 px-4 pb-8">{open ? children : null}</div>
      </SheetContent>
    </Sheet>
  )
}
