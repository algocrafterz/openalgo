import { webClient } from './client'

export interface OpsAction {
  key: string
  label: string
  detail: string
  available: boolean
  /** Why it cannot start now, in words for a trader. Empty when it can. */
  reason: string
}

export interface OpsRun {
  id: string
  action: string
  day: string
  label: string
  started: string
  ended?: string
  outcome?: 'ok' | 'failed'
  message?: string
  tail?: string
}

export interface OpsActionsView {
  actions: OpsAction[]
  run: OpsRun | null
}

function message(error: unknown, fallback: string): string {
  const e = error as { response?: { data?: { message?: string } } }
  return e.response?.data?.message ?? fallback
}

export const opsActionsApi = {
  list: async (day: string): Promise<OpsActionsView> => {
    try {
      const res = await webClient.get<{ data?: OpsActionsView }>('/api/ops/actions', {
        params: { day },
      })
      return res.data.data ?? { actions: [], run: null }
    } catch {
      // No buttons is the safe answer when the list cannot be read.
      return { actions: [], run: null }
    }
  },
  run: async (action: string, day: string): Promise<OpsRun> => {
    try {
      const res = await webClient.post<{ run: OpsRun }>('/api/ops/run-action', { action, day })
      return res.data.run
    } catch (error) {
      throw new Error(message(error, 'It could not be started. Try again in a moment.'))
    }
  },
}
