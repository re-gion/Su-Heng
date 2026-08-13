export type CreatedTask = {
  task_id: string
  status: string
  events_url: string
  created_at: string
}

type ErrorEnvelope = { error?: { message?: string } }

export type TaskListItem = {
  task_id: string
  event_query: string
  status: string
  resumable: boolean
  report_id: string | null
}

export type DataStatus = {
  demo_mode: boolean
  assets: number
  historical_events: number
  hot_snapshots: number
  hot_coverage: { from: string | null; to: string | null }
  scheduler: { configured: boolean; last_run: string | null; last_result: unknown }
  report_ttl_hours: number | null
}

export async function getDataStatus(): Promise<DataStatus> {
  const response = await fetch('/api/data/status')
  if (!response.ok) throw new Error('无法读取本地数据层状态')
  return response.json() as Promise<DataStatus>
}

export async function createTask(eventQuery: string, depth = 'standard', userNote = '', timeFrom = '', timeTo = ''): Promise<CreatedTask> {
  const response = await fetch('/api/tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      event_query: eventQuery,
      depth,
      user_note: userNote || null,
      time_range: timeFrom || timeTo ? { from: timeFrom || null, to: timeTo || null } : null,
    }),
  })
  if (!response.ok) {
    const body = (await response.json()) as ErrorEnvelope
    throw new Error(body.error?.message ?? `创建失败（HTTP ${response.status}）`)
  }
  return (await response.json()) as CreatedTask
}

async function mutation(path: string, method = 'POST', body?: unknown): Promise<unknown> {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!response.ok) {
    const payload = (await response.json()) as ErrorEnvelope
    throw new Error(payload.error?.message ?? `操作失败（HTTP ${response.status}）`)
  }
  return response.json()
}

export const pauseTask = (taskId: string) => mutation(`/api/tasks/${taskId}/pause`)
export const stopTask = (taskId: string) => mutation(`/api/tasks/${taskId}/stop`, 'POST', { reason: 'user_stop' })
export const deleteTask = (taskId: string) => mutation(`/api/tasks/${taskId}`, 'DELETE')

export type PublicConfig = {
  llm: {
    default: { api_key: string | null; base_url: string; model: string }
    roles: Record<string, { api_key: string | null; base_url: string | null; model: string | null; effective: { api_key: string | null; base_url: string; model: string }; source: Record<string, string> }>
  }
  search: { provider_order: string[]; keys: Record<string, string | null> }
}

export async function getConfig(): Promise<PublicConfig> {
  const response = await fetch('/api/config')
  if (!response.ok) throw new Error('无法读取配置')
  return response.json() as Promise<PublicConfig>
}

export async function updateConfig(payload: unknown): Promise<PublicConfig> {
  return mutation('/api/config', 'PUT', payload) as Promise<PublicConfig>
}

export async function testConfig(payload: { kind: 'llm'; role: string } | { kind: 'search'; provider: string }): Promise<{ ok: boolean; sample?: string; error?: { message?: string }; latency_ms: number }> {
  return mutation('/api/config/test', 'POST', payload) as Promise<{ ok: boolean; sample?: string; error?: { message?: string }; latency_ms: number }>
}

export async function listTasks(): Promise<TaskListItem[]> {
  const response = await fetch('/api/tasks?limit=8')
  if (!response.ok) throw new Error('无法读取任务列表')
  return ((await response.json()) as { items: TaskListItem[] }).items
}

export async function resumeTask(taskId: string): Promise<void> {
  const response = await fetch(`/api/tasks/${taskId}/resume`, { method: 'POST' })
  if (!response.ok) {
    const body = (await response.json()) as ErrorEnvelope
    throw new Error(body.error?.message ?? `续跑失败（HTTP ${response.status}）`)
  }
}
