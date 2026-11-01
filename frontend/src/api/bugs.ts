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
  verification: string
  sources: string[]
  history: BugHistoryEntry[]
  created_at: string
  updated_at: string
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
}
