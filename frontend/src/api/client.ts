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

export async function createTask(eventQuery: string): Promise<CreatedTask> {
  const response = await fetch('/api/tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ event_query: eventQuery, depth: 'quick' }),
  })
  if (!response.ok) {
    const body = (await response.json()) as ErrorEnvelope
    throw new Error(body.error?.message ?? `创建失败（HTTP ${response.status}）`)
  }
  return (await response.json()) as CreatedTask
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
