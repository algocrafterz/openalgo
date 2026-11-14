import { webClient } from './client'

export type BugKind = 'BUG' | 'EXECUTION' | 'DATA' | 'TUNING-IDEA'
export type BugSeverity = 'HIGH' | 'MEDIUM' | 'LOW'
export type BugStatus = 'open' | 'in_progress' | 'fixed' | 'verified' | 'wontfix'

export interface BugHistoryEntry {
  at: string
  status: BugStatus
  note: string
}

export interface Bug {
  id: string
  title: string
  plain?: string
  kind: BugKind
  severity: BugSeverity
  status: BugStatus
  area: string
  strategies: string[]
  first_seen: string
  last_seen: string
  occurrences: string[]
  impact: string
  evidence: string[]
  root_cause: string
  fix: string
  fix_commit: string
  fix_explanation?: string
  verification: string
  sources: string[]
  history: BugHistoryEntry[]
  created_at: string
  updated_at: string
  /** The fields below are computed by the server on every read; they are never stored. */
  owner?: string
  age_days?: number | null
  overdue?: boolean
  awaiting_verification?: boolean
  next_action?: string
}

export type BugActionKey = 'assign' | 'start' | 'note' | 'fix' | 'verify' | 'reopen' | 'wontfix'

/** Only free text is sent. The server decides the status from the action and checks every rule. */
export interface BugActionBody {
  owner?: string
  text?: string
  fix?: string
  commit?: string
  fix_explanation?: string
  evidence?: string
  reason?: string
}

interface BugListResponse {
  status: 'success' | 'error'
  data?: Bug[]
  message?: string
}

export const bugsApi = {
  list: async (): Promise<Bug[]> => {
    try {
      const res = await webClient.get<BugListResponse>('/api/bugs')
      return res.data.data ?? []
    } catch (error) {
      const axiosError = error as { response?: { data?: BugListResponse } }
      throw new Error(axiosError.response?.data?.message ?? 'The bug list could not be loaded.')
    }
  },
  act: async (id: string, action: BugActionKey, body: BugActionBody): Promise<Bug> => {
    try {
      const res = await webClient.post<{ status: string; data: Bug }>(
        `/api/bugs/${encodeURIComponent(id)}/${action}`,
        body
      )
      return res.data.data
    } catch (error) {
      const axiosError = error as { response?: { data?: { message?: string } } }
      throw new Error(
        axiosError.response?.data?.message ?? 'The bug could not be updated. Try again in a moment.'
      )
    }
  },
}
