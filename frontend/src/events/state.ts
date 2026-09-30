export type TaskEvent = {
  event: string
  task_id: string
  seq: number
  ts: string
  data: Record<string, unknown>
}

export type TaskTiming = { available: boolean; active_seconds: number | null; waiting_seconds: number | null; phase_seconds: Record<string, number>; sampled_at?: string; status?: string }

export function advanceTiming(timing: TaskTiming, at: string): TaskTiming {
  const seconds = Math.max(0, (Date.parse(at) - Date.parse(timing.sampled_at ?? at)) / 1000) || 0
  const active = ['running', 'pausing', 'stopping'].includes(timing.status ?? '')
  return { ...timing, sampled_at: at,
    active_seconds: (timing.active_seconds ?? 0) + (active ? seconds : 0),
    waiting_seconds: (timing.waiting_seconds ?? 0) + (timing.status === 'paused' ? seconds : 0),
  }
}

export type EvidenceItem = {
  id: string
  title: string
  source: string
  tier: number
}

export type StreamState = {
  lastSeq: number
  lastEventAt?: string
  timing?: TaskTiming
  verification: { claims: Record<string, string>; total: number }
  status: string
  phase: string
  agentPhase: string
  agents: Record<string, string>
  evidence: EvidenceItem[]
  forum: ForumItem[]
  hostReviews: HostReview[]
  budget: { tokensUsed: number; tokensLimit: number; calls: number; timing?: TaskTiming; tokensReserved?: number; phaseTokenLimit?: number }
  degradations: string[]
  decisions: string[]
  reportUrl: string | null
  errors: string[]
}

export type ForumSummaryItem = { text: string; claimRef: string; evidenceRefs: string[] }
export type ForumItem = {
  agent: string
  type: string
  content: string
  round: number
  phase: string
  targetAgent: string
  refs: string[]
  summaryItems: ForumSummaryItem[]
}
export type HostReview = { release: boolean; reason: string; gaps: Array<{ desc?: string; priority?: string }> }

export const initialStreamState: StreamState = {
  lastSeq: 0,
  verification: { claims: {}, total: 0 },
  status: 'queued',
  phase: 'planning',
  agentPhase: '等待启动',
  agents: {},
  evidence: [],
  forum: [],
  hostReviews: [],
  budget: { tokensUsed: 0, tokensLimit: 0, calls: 0 },
  degradations: [],
  decisions: [],
  reportUrl: null,
  errors: [],
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function textList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && item.length > 0) : []
}

function forumSummaryItems(value: unknown): ForumSummaryItem[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((item) => {
    if (typeof item !== 'object' || item === null) return []
    const row = item as Record<string, unknown>
    const content = text(row.text).trim()
    if (!content) return []
    return [{ text: content, claimRef: text(row.claim_ref), evidenceRefs: textList(row.evidence_refs) }]
  })
}

const providerLabels: Record<string, string> = {
  langsearch: 'LangSearch',
  qianfan: '千帆',
  bocha: '博查',
  exa: 'Exa',
  tavily: 'Tavily',
  serper: 'Serper',
}

function providerLabel(value: unknown): string {
  const name = text(value)
  return providerLabels[name] ?? name
}

function providerGap(provider: string): string {
  return /[A-Za-z0-9]$/.test(provider) ? ' ' : ''
}

function numberValue(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function searchRejectionSummary(value: unknown): string {
  if (!value || typeof value !== 'object') return ''
  const labels: Record<string, string> = {
    subject_mismatch: '主体不符',
    event_mismatch: '非当前事件',
    foreign_source_in_domestic_phase: '境外来源不符合当前国内范围',
    language_not_allowed: '语言不在允许范围',
    current_event_not_history: '本事件材料不能充当历史对照',
    history_query_mismatch: '不符合历史案例查询',
  }
  const reasons = Object.entries(value)
    .filter(([, count]) => numberValue(count) > 0)
    .map(([reason, count]) => `${labels[reason] ?? '其他范围限制'} ${numberValue(count)} 条`)
  return reasons.length ? `（${reasons.join('、')}）` : ''
}

function searchRoutingSummary(data: Record<string, unknown>): string | null {
  const isHistory = text(data.agent) === 'history_insight'
  if (!Array.isArray(data.provider_diagnostics)) {
    const from = providerLabel(data.degraded_from)
    const to = providerLabel(data.provider)
    return from && to ? `${from} 调用失败，已改用 ${to}（历史事件未记录具体原因）。` : null
  }
  const diagnostics = data.provider_diagnostics.filter(
    (item): item is Record<string, unknown> => typeof item === 'object' && item !== null,
  )
  const hasRouting = diagnostics.length > 1 || diagnostics.some((item) => item.status !== 'success')
  if (!hasRouting) return null
  const parts: string[] = []
  for (const item of diagnostics) {
    const provider = providerLabel(item.provider)
      + (item.recovery_reason === 'relax_preferred_date_window' ? '（放宽日期补查）' : '')
    const gap = providerGap(provider)
    const status = text(item.status)
    const count = numberValue(item.count)
    const accepted = numberValue(item.accepted_count)
    if (status === 'insufficient_coverage') {
      parts.push(`${provider}${gap}筛选后保留 ${accepted} 条，但只覆盖 ${numberValue(item.source_groups)} 个候选发布主体，尝试用 Exa 补充`)
    } else if (status === 'relevance_filtered_empty') {
      parts.push(`${provider}${gap}返回 ${count} 条，但均未通过${isHistory ? '历史对照初筛' : '事件相关性筛选'}${searchRejectionSummary(item.rejected)}`)
    } else if (status === 'filtered_empty') {
      parts.push(`${provider}${gap}返回 ${count} 条，但均未通过域名范围筛选`)
    } else if (status === 'empty') {
      parts.push(`${provider}${gap}未返回结果`)
    } else if (status === 'budget_exhausted') {
      parts.push(`${provider}${gap}搜索调用预算已用完，本次未调用上游接口`)
    } else if (
      status === 'error'
      && (text(item.reason) === 'local_quota_guard' || text(item.error_type) === 'ProviderQuotaExceeded')
    ) {
      parts.push(`${provider}${gap}达到本地额度保护线，本次未调用上游接口`)
    } else if (status === 'error' || status === 'invalid_response') {
      const errorType = text(item.error_type)
      parts.push(`${provider}${gap}调用失败${errorType ? `（${errorType}）` : ''}`)
    } else if (status === 'skipped' && text(item.reason) === 'breaker_open') {
      parts.push(`${provider}${gap}暂时跳过（熔断保护）`)
    } else if (status === 'skipped' && text(item.reason) === 'task_limit') {
      parts.push(`${provider}${gap}已达到本任务调用上限`)
    } else if (status === 'skipped') {
      parts.push(`${provider}${gap}因能力不匹配而跳过`)
    } else if (status === 'success' && diagnostics.length > 1) {
      parts.push(isHistory
        ? `${provider}${gap}有 ${accepted} 条历史对照候选材料通过初筛，仍需核验来源`
        : `${provider}${gap}获得 ${accepted} 条符合事件范围的材料`)
    }
  }
  if (numberValue(data.hits) === 0) {
    parts.push(isHistory ? '本次查询未找到历史对照候选材料' : '本次查询未找到符合事件范围的材料')
  }
  return parts.length ? `${parts.join('；')}。` : null
}

export function applyEvent(state: StreamState, message: TaskEvent): StreamState {
  if (message.event === '__reset__') return initialStreamState
  if (message.seq <= state.lastSeq) return state
  const next = { ...state, lastSeq: message.seq, lastEventAt: message.ts }
  if (message.event === 'task.status') {
    next.timing = {
      ...advanceTiming(state.timing ?? { available: true, active_seconds: 0, waiting_seconds: 0, phase_seconds: {} }, message.ts),
      status: text(message.data.status),
    }
    next.status = text(message.data.status)
    next.phase = text(message.data.phase)
  } else if (message.event === 'verify.progress') {
    const id = text(message.data.claim_id)
    next.verification = {
      claims: id ? { ...state.verification.claims, [id]: text(message.data.verification_state) || 'unknown' } : state.verification.claims,
      total: Math.max(state.verification.total, numberValue(message.data.total)),
    }
  } else if (message.event === 'agent.status') {
    next.agentPhase = text(message.data.phase)
    const agent = text(message.data.agent)
    if (agent) next.agents = { ...state.agents, [agent]: text(message.data.phase) }
  } else if (message.event === 'evidence.added') {
    const id = text(message.data.evidence_id)
    if (!state.evidence.some((item) => item.id === id)) {
      next.evidence = [
        ...state.evidence,
        {
          id,
          title: text(message.data.title),
          source: text(message.data.source_name) || '未识别来源',
          tier: typeof message.data.source_tier === 'number' ? message.data.source_tier : 4,
        },
      ]
    }
  } else if (message.event === 'loop.round') {
    const reasons: Record<string, string> = { approved: '主持人批准结束', round_limit: '达到讨论轮次上限，转入质量评估', budget_exhausted: '预算不足，调查结束', verification_reserve: '调查达到阶段预算上限，转入核验与报告', no_progress: '连续补查无新增有效结果', user_stop: '用户停止', core_complete: '核心内容已达到发布要求' }
    const decisions: Record<string, string> = { start: '开始调查', release: '主持人批准结束', force_release: '讨论因限制结束，未获主持人批准', continue: '继续调查', stop: '结束本轮调查' }
    const label = reasons[text(message.data.end_reason)] ?? decisions[text(message.data.decision)] ?? text(message.data.decision)
    next.decisions = [...state.decisions, `${message.data.scope === 'quality_recovery' ? '专项补查 · ' : ''}${label}：${text(message.data.reason)}`]
  } else if (message.event === 'report.done') {
    next.reportUrl = text(message.data.html_url)
  } else if (message.event === 'error') {
    next.errors = [...state.errors, text(message.data.message)]
  } else if (message.event === 'forum.message') {
    const payload = typeof message.data.payload === 'object' && message.data.payload !== null
      ? message.data.payload as Record<string, unknown> : {}
    next.forum = [...state.forum, {
      agent: text(message.data.agent),
      type: text(message.data.type),
      content: text(message.data.content),
      round: typeof message.data.round === 'number' ? message.data.round : 0,
      phase: text(payload.phase),
      targetAgent: text(payload.agent),
      refs: textList(message.data.refs),
      summaryItems: forumSummaryItems(payload.summary_items),
    }]
  } else if (message.event === 'host.review') {
    next.hostReviews = [...state.hostReviews, {
      release: message.data.release === true,
      reason: text(message.data.reason),
      gaps: Array.isArray(message.data.gaps) ? message.data.gaps as Array<{ desc?: string; priority?: string }> : [],
    }]
  } else if (message.event === 'budget.update') {
    if (message.data.timing && typeof message.data.timing === 'object') {
      const timing = message.data.timing as TaskTiming
      next.timing = { ...timing, sampled_at: timing.sampled_at ?? message.ts, status: timing.status ?? state.status }
    }
    next.budget = {
      tokensUsed: typeof message.data.tokens_used === 'number' ? message.data.tokens_used : 0,
      tokensLimit: typeof message.data.tokens_limit === 'number' ? message.data.tokens_limit : 0,
      calls: typeof message.data.calls === 'number' ? message.data.calls : 0,
      ...(message.data.timing && typeof message.data.timing === 'object' ? { timing: message.data.timing as TaskTiming } : {}),
      ...(typeof message.data.tokens_reserved === 'number' ? { tokensReserved: message.data.tokens_reserved } : {}),
      ...(typeof message.data.phase_token_limit === 'number' ? { phaseTokenLimit: message.data.phase_token_limit } : {}),
    }
  } else if (message.event === 'search.result') {
    const summary = searchRoutingSummary(message.data)
    if (summary) next.degradations = [...state.degradations, summary]
  } else if (message.event === 'warning') {
    next.degradations = [...state.degradations, text(message.data.message)]
  }
  return next
}
