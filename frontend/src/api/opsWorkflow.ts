import { webClient } from './client'

export interface WorkflowStep {
  step: string
  started: string
  ended: string
  exit: number
}

export interface WorkflowPhase {
  phase: 'open-day' | 'liveness' | 'close-day'
  steps: WorkflowStep[] | null
}

export interface WorkflowCheck {
  name: string
  status: 'OK' | 'WARN' | 'FAIL' | 'NOT VERIFIED'
  evidence: string
}

export interface WorkflowCheckResult {
  phase: string
  day: string
  verdict: 'GREEN' | 'AMBER' | 'RED'
  summary: string
  checks: WorkflowCheck[]
}

export interface WorkflowDay {
  day: string
  phases: WorkflowPhase[]
  checks: Record<string, WorkflowCheckResult>
  reports: string[]
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
        data: res.data.data ?? { day: '', phases: [], checks: {}, reports: [] },
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
