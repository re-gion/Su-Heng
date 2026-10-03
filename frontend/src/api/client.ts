export type CreatedTask = {
  task_id: string
  status: string
  events_url: string
  created_at: string
  investigation_scope?: 'general' | 'institution' | 'public_event'
}

type ErrorEnvelope = { error?: { code?: string; message?: string; details?: { scope_label?: string; proposed_scope?: string } } }

export class TaskCreationError extends Error {
  constructor(public code: string, message: string, public scopeLabel?: string) {
    super(message)
  }
}

export type TaskListItem = {
  task_id: string
  event_query: string
  resolved_event_query: string | null
  status: string
  phase: string
  resumable: boolean
  comment_selection_required: boolean
  topic_selection_required: boolean
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

export type CreateTaskOptions = {
  sourceScope: 'auto' | 'domestic' | 'global'
  sourceLanguages: string[]
  commentMode: 'off' | 'smart' | 'manual' | 'hybrid'
  commentUrls: string[]
  investigationScope?: 'general' | 'institution' | 'public_event'
}

export async function createTask(eventQuery: string, depth = 'standard', userNote = '', timeFrom = '', timeTo = '', options: CreateTaskOptions = { sourceScope: 'auto', sourceLanguages: ['zh', 'en'], commentMode: 'off', commentUrls: [] }): Promise<CreatedTask> {
  const response = await fetch('/api/tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      event_query: eventQuery,
      depth,
      user_note: userNote || null,
      investigation_scope: options.investigationScope ?? 'general',
      time_range: timeFrom || timeTo ? { from: timeFrom || null, to: timeTo || null } : null,
      source_scope: options.sourceScope,
      source_languages: options.sourceLanguages,
      comment_mode: options.commentMode,
      comment_urls: options.commentUrls,
    }),
  })
  if (!response.ok) {
    const body = (await response.json()) as ErrorEnvelope
    throw new TaskCreationError(body.error?.code ?? 'CREATE_FAILED', body.error?.message ?? `创建失败（HTTP ${response.status}）`, body.error?.details?.scope_label)
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

export type BudgetTable = Record<string, Record<string, number>>

export type ProviderQuotaWindow = {
  period: string
  period_key: string
  unit: string
  used: number
  normal_limit: number | null
  critical_limit: number | null
  normal_remaining: number | null
  critical_remaining: number | null
}

export type ProviderQuotaStatus = {
  state: 'available' | 'normal_limit_reached' | 'critical_limit_reached' | 'metered_without_fixed_limit'
  upstream_quota_verified: boolean
  activity?: { day_calls: number; month_calls: number }
  windows: ProviderQuotaWindow[]
}

export type PublicConfig = {
  llm: {
    default: { api_key: string | null; base_url: string; model: string }
    roles: Record<string, { api_key: string | null; base_url: string | null; model: string | null; effective: { api_key: string | null; base_url: string; model: string }; source: Record<string, string> }>
  }
  search: { provider_order: string[]; keys: Record<string, string | null>; quota: Record<string, ProviderQuotaStatus> }
  fetch: { provider_order: string[]; keys: Record<string, string | null>; quota?: Record<string, ProviderQuotaStatus> }
  comments: { enabled: boolean }
  budget: {
    overrides: BudgetTable
    effective: BudgetTable
    defaults: BudgetTable
    fields: string[]
    depths: string[]
    source: string
  }
}

export type CommentPlatformStatus = { platform: string; profile_present: boolean; browser_open: boolean }
export type CommentPluginStatus = { enabled: boolean; available: boolean; demo_mode: boolean; container: boolean; platforms: CommentPlatformStatus[]; risk_notice: string }
export type CommentCandidate = { id: string; url: string; platform: string; title: string; snippet: string | null; score: number; score_breakdown: Record<string, number>; reasons: string[]; selection_mode: string; status: string; login_profile_present: boolean }
export type CommentCandidates = { task_id: string; phase: string; items: CommentCandidate[]; budgets: { posts: number; comments_per_post: number }; query?: string | null; discovery_attempts: { platform: string; status: 'found' | 'empty' | 'failed'; count: number; error?: string | null }[]; manual_entry_allowed: boolean }
export type TopicSource = { url: string; title: string; source_name: string; published_at: string | null; role: string; provider: string }
export type TopicCandidate = {
  id: string
  title: string
  query: string
  summary: string
  confidence: 'confirmed' | 'lead'
  confidence_label: string
  score: number
  reasons: string[]
  gaps: string[]
  sources: TopicSource[]
  source_count: number
  date_from: string | null
  date_to: string | null
  coverage_limited: boolean
  source_name: string
  url: string
  published_at: string | null
  date_status: string
}
export type TopicDiscoveryAttempt = { round: 'initial' | 'recovery' | 'manual_preflight'; query: string; language: string; provider: string; status: 'found' | 'empty' | 'failed' | 'budget_exhausted'; raw_hits: number; accepted_hits: number; rejected: Record<string, number>; error?: string | null }
export type TopicCandidates = {
  task_id: string
  phase: string
  original_query: string
  items: TopicCandidate[]
  attempts: TopicDiscoveryAttempt[]
  provider_coverage: { configured: number; attempted: string[]; successful: string[]; limited: boolean; message?: string | null } | null
  effective_time_range: { date_from: string; date_to: string } | null
  used_default_time_range: boolean
  manual_preflight: { status: 'verified' | 'unverified'; query?: string; message?: string } | null
  manual_entry_allowed: boolean
}

export async function getCommentPluginStatus(): Promise<CommentPluginStatus> {
  const response = await fetch('/api/comment-plugin/status')
  if (!response.ok) throw new Error('无法读取评论插件状态')
  return response.json() as Promise<CommentPluginStatus>
}
export const openCommentLogin = (platform: string) => mutation(`/api/comment-plugin/platforms/${platform}/login`)
export const clearCommentProfile = (platform: string) => mutation(`/api/comment-plugin/platforms/${platform}/profile`, 'DELETE', { confirm: true })
export async function getCommentCandidates(taskId: string): Promise<CommentCandidates> {
  const response = await fetch(`/api/tasks/${taskId}/comment-candidates`)
  if (!response.ok) throw new Error('无法读取评论候选')
  return response.json() as Promise<CommentCandidates>
}
export const submitCommentSelection = (taskId: string, payload: { action: 'approve' | 'skip'; candidate_ids?: string[]; urls?: string[] }) => mutation(`/api/tasks/${taskId}/comment-selection`, 'POST', payload)
export const stopCommentCollection = (taskId: string) => mutation(`/api/tasks/${taskId}/comment-collection/stop`)
export async function getTopicCandidates(taskId: string): Promise<TopicCandidates> {
  const response = await fetch(`/api/tasks/${taskId}/topic-candidates`)
  if (!response.ok) throw new Error('无法读取具体事件候选')
  return response.json() as Promise<TopicCandidates>
}
export const submitTopicSelection = (taskId: string, payload: { candidate_id?: string; event_query?: string; force?: boolean }) => mutation(`/api/tasks/${taskId}/topic-selection`, 'POST', payload)
export const expandTopicDiscovery = (taskId: string) => mutation(`/api/tasks/${taskId}/topic-discovery`, 'POST', { window: 'three_years' })

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

export type TaskDetail = {
  timing?: import('../events/state').TaskTiming
  call_diagnostics?: { recorded_requests: number; recorded_tokens: number; queue_ms: number; request_ms: number }
  investigation_scope?: 'general' | 'institution' | 'public_event'
  investigation_outcome?: { end_reason?: string } | null
  recovery?: { round?: number; no_gain?: number; end_reason?: string; missing?: string[] } | null
  chapter_status?: Record<string, { status: string; message?: string }>
  release_label?: string | null
  task_id: string
  event_query: string
  resolved_event_query: string | null
  status: string
  phase: string
  resumable: boolean
  comment_selection_required: boolean
  topic_selection_required: boolean
  report_id: string | null
}

export type TaskProgressSnapshot = {
  task_id: string
  seq: number
  status: string
  phase: string
  timing: import('../events/state').TaskTiming
  budget?: { tokens_used: number; calls: number; tokens_limit: number; tokens_reserved: number }
  last_event_at: string | null
  verification: { total: number; complete?: number; incomplete?: number; skipped?: number; pending?: number }
  model_calls: {
    recorded_requests: number; recorded_tokens: number; queue_ms: number; request_ms: number
    queued_requests: number; inflight_requests: number; failed_requests: number; retry_requests: number
    activities: Array<{ stage: string; role: string; status: string; attempt: number; elapsed_seconds: number }>
    by_stage?: Record<string, { requests: number; failed: number; retries: number; request_ms: number; queue_ms: number }>
  }
}

export async function getTaskProgress(taskId: string, signal: AbortSignal): Promise<TaskProgressSnapshot> {
  const response = await fetch(`/api/tasks/${taskId}/progress`, { signal })
  if (!response.ok) throw new Error(`进度校准失败（HTTP ${response.status}）`)
  return response.json() as Promise<TaskProgressSnapshot>
}

export async function getTaskDetail(taskId: string): Promise<TaskDetail> {
  const response = await fetch(`/api/tasks/${taskId}`)
  if (!response.ok) throw new Error('无法读取任务详情')
  return response.json() as Promise<TaskDetail>
}

export async function resumeTask(taskId: string): Promise<void> {
  const response = await fetch(`/api/tasks/${taskId}/resume`, { method: 'POST' })
  if (!response.ok) {
    const body = (await response.json()) as ErrorEnvelope
    throw new Error(body.error?.message ?? `续跑失败（HTTP ${response.status}）`)
  }
}

export type CommentQuestionData = {
  block: null | {
    analysis_mode?: string
    quick_read_status?: string
    coverage: Record<string, number>
    samples: { id: string; text: string; platform: string; source_url: string; published_at?: string | null }[]
    observations: { id: string; text: string; stance: string; comment_refs: string[] }[]
    items: { id: string; title: string; summary?: string | null; sample_count: number; observation_refs: string[]; publicly_verifiable?: boolean;
      comparisons: { id: string; status: string; text: string; evidence_refs?: string[] }[];
      judgements: { id: string; priority: string; response_action: string; uncertainty: string; evidence_refs?: string[] }[] }[]
  }
  jobs: Record<string, { status: string; mode?: string; message?: string }>
  sources?: Record<string, { title: string; url: string }>
  deepening_available: boolean
}
export async function getCommentQuestions(taskId: string, signal?: AbortSignal): Promise<CommentQuestionData> {
  const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/comment-questions`, { signal })
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    throw new Error(payload?.error?.message ?? '无法读取评论速读')
  }
  return response.json() as Promise<CommentQuestionData>
}
export const deepenCommentQuestion = (taskId: string, questionId: string, mode: 'existing' | 'follow_up') =>
  mutation(`/api/tasks/${encodeURIComponent(taskId)}/comment-questions/${encodeURIComponent(questionId)}/analyze`, 'POST', { mode })
