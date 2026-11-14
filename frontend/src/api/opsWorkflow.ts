import { webClient } from './client'

export interface WorkflowStep {
  step: string
  started: string
  ended: string
  exit: number
  /** True when the step was run again from the page; this is its latest result. */
  reran?: boolean
}

export interface WorkflowPhase {
  phase: 'open-day' | 'liveness' | 'close-day'
  steps: WorkflowStep[] | null
}

export interface WorkflowCheck {
  name: string
  status: 'OK' | 'WARN' | 'FAIL' | 'NOT VERIFIED'
  evidence: string
  plain?: string
}

export interface WorkflowCheckResult {
  phase: string
  day: string
  verdict: 'GREEN' | 'AMBER' | 'RED'
  summary: string
  checks: WorkflowCheck[]
}

export interface WorkflowVerdict {
  verdict: 'GREEN' | 'AMBER' | 'RED'
  line: string
}

/** What the close-day review achieved, read from its report and not from its exit code. */
export interface WorkflowReview {
  outcome: 'verdict' | 'no_verdict' | 'limit_or_auth' | 'failed'
  cause: string
  text: string
  exit: number | null
  note: string
}

/** One expected phase. `state` is a manifest state or a reader-derived one (see workflow_status.py). */
export interface WorkflowRunStatus {
  phase: 'open-day' | 'liveness' | 'close-day'
  due: string
  state: string
  /** Started after its due time plus the margin. */
  late: boolean
  started_at: string | null
  ended_at: string | null
  note: string
}

export interface WorkflowRecurrenceItem {
  group: string
  name: string
  count: number
  first: string
  last: string
  failing_today: boolean
}

export interface WorkflowRecurrence {
  /** Sessions looked at (at most 10). */
  window: number
  items: WorkflowRecurrenceItem[]
}

/** Last alert and recovery. Never holds a token, chat id or message body. */
export interface WorkflowNotifications {
  available: boolean
  reason?: string
  last_attempt_at?: string | null
  last_result?: string | null
  last_key?: string | null
  last_sent_at?: string | null
  last_sent_key?: string | null
  open_red?: string[]
  last_recovery_at?: string | null
  last_recovery_key?: string | null
}

export interface WorkflowDay {
  day: string
  phases: WorkflowPhase[]
  checks: Record<string, WorkflowCheckResult>
  reports: string[]
  verdict: WorkflowVerdict | null
  /** Absent in an older server response: the exit code then decides, as before. */
  review?: WorkflowReview
  /** The three fields below are absent in an older server response. */
  run_status?: WorkflowRunStatus[]
  recurrence?: WorkflowRecurrence
  notifications?: WorkflowNotifications
}

interface WorkflowResponse {
  status: 'success' | 'error'
  days?: string[]
  data?: WorkflowDay
  message?: string
}

interface ReportResponse {
  status: 'success' | 'error'
  data?: string
  message?: string
}

function errorMessage(error: unknown, fallback: string): string {
  const axiosError = error as { response?: { data?: { message?: string } } }
  return axiosError.response?.data?.message ?? fallback
}

export const opsWorkflowApi = {
  get: async (day?: string): Promise<{ days: string[]; data: WorkflowDay }> => {
    try {
      const res = await webClient.get<WorkflowResponse>('/api/ops/workflow', {
        params: day ? { day } : undefined,
      })
      return {
        days: res.data.days ?? [],
        data: res.data.data ?? { day: '', phases: [], checks: {}, reports: [], verdict: null },
      }
    } catch (error) {
      throw new Error(errorMessage(error, 'The workflow status could not be loaded.'))
    }
  },
  report: async (name: string): Promise<string> => {
    try {
      const res = await webClient.get<ReportResponse>('/api/ops/report', { params: { name } })
      return res.data.data ?? ''
    } catch (error) {
      throw new Error(errorMessage(error, 'The report could not be loaded.'))
    }
  },
}
