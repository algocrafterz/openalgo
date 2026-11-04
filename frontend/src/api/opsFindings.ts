import type { ReportPayload } from '@/lib/reportFinding'
import type { Bug } from './bugs'
import { webClient } from './client'

export interface FindingLink {
  phase: string
  group: string
  name: string
  bug_id: string
  status: Bug['status']
}

export interface ReportResult {
  /** True when a new bug was written. */
  created: boolean
  /** False when the day was already on the bug, so nothing changed. */
  recorded: boolean
  bug: Bug
}

function message(error: unknown, fallback: string): string {
  const e = error as { response?: { data?: { message?: string } }; message?: string }
  return e.response?.data?.message ?? fallback
}

export const opsFindingsApi = {
  links: async (day: string): Promise<FindingLink[]> => {
    try {
      const res = await webClient.get<{ data?: FindingLink[] }>('/api/ops/finding-links', {
        params: { day },
      })
      return res.data.data ?? []
    } catch {
      // A missing link list must not hide the findings; they just show "Report as bug".
      return []
    }
  },
  report: async (payload: ReportPayload): Promise<ReportResult> => {
    try {
      const res = await webClient.post<ReportResult & { status: string }>(
        '/api/ops/report-finding',
        payload
      )
      return res.data
    } catch (error) {
      throw new Error(message(error, 'The bug could not be recorded. Try again in a moment.'))
    }
  },
}
