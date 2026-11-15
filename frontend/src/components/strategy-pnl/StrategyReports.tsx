import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, Download, ExternalLink } from 'lucide-react'
import { strategyPnlApi, strategyReportUrl } from '@/api/strategyPnl'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'

const MODE_LABEL = { paper: 'Paper', live: 'Live', both: 'Paper and live' } as const

/** Saved strategy performance reports: date, mode, View (new tab) and Download. */
export function StrategyReports() {
  const query = useQuery({
    queryKey: ['strategy-reports'],
    queryFn: strategyPnlApi.listReports,
    staleTime: 60_000,
  })
  const reports = query.data ?? []

  return (
    <Card>
      <CardHeader>
        <CardTitle>Strategy Reports</CardTitle>
        <p className="text-sm text-muted-foreground">
          Weekly performance reports, saved after Friday&apos;s close. Each report is a snapshot of
          the day it was made.
        </p>
      </CardHeader>
      <CardContent>
        {query.isLoading && <p className="text-sm text-muted-foreground">Loading reports...</p>}
        {query.isError && (
          <div
            role="alert"
            className="flex items-center gap-2 text-sm text-red-600 dark:text-red-400"
          >
            <AlertTriangle className="h-4 w-4" />
            <span>
              {query.error instanceof Error ? query.error.message : 'Could not load reports.'}
            </span>
          </div>
        )}
        {!query.isLoading && !query.isError && reports.length === 0 && (
          <p className="text-sm text-muted-foreground">No reports have been saved yet.</p>
        )}
        {reports.length > 0 && (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Date</TableHead>
                <TableHead>Mode</TableHead>
                <TableHead className="text-right">Report</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {reports.map((r) => (
                <TableRow key={r.filename}>
                  <TableCell>{r.date}</TableCell>
                  <TableCell>{MODE_LABEL[r.mode] ?? r.mode}</TableCell>
                  <TableCell className="text-right space-x-4">
                    <a
                      className="inline-flex items-center gap-1 text-primary hover:underline"
                      href={strategyReportUrl(r.filename)}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      <ExternalLink className="h-3.5 w-3.5" />
                      View
                    </a>
                    <a
                      className="inline-flex items-center gap-1 text-primary hover:underline"
                      href={strategyReportUrl(r.filename, true)}
                      download={r.filename}
                    >
                      <Download className="h-3.5 w-3.5" />
                      Download
                    </a>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  )
}
