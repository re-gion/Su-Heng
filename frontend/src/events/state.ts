export type TaskEvent = {
  event: string
  task_id: string
  seq: number
  ts: string
  data: Record<string, unknown>
}

export type EvidenceItem = {
  id: string
  title: string
  source: string
  tier: number
}

export type StreamState = {
  lastSeq: number
  status: string
  phase: string
  agentPhase: string
  agents: Record<string, string>
  evidence: EvidenceItem[]
  forum: ForumItem[]
  hostReviews: HostReview[]
  budget: { tokensUsed: number; tokensLimit: number; calls: number }
  degradations: string[]
  decisions: string[]
  reportUrl: string | null
  errors: string[]
}

export type ForumItem = { agent: string; type: string; content: string; round: number }
export type HostReview = { release: boolean; reason: string; gaps: Array<{ desc?: string; priority?: string }> }

export const initialStreamState: StreamState = {
  lastSeq: 0,
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

export function applyEvent(state: StreamState, message: TaskEvent): StreamState {
  if (message.event === '__reset__') return initialStreamState
  if (message.seq <= state.lastSeq) return state
  const next = { ...state, lastSeq: message.seq }
  if (message.event === 'task.status') {
    next.status = text(message.data.status)
    next.phase = text(message.data.phase)
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
    next.decisions = [...state.decisions, `${text(message.data.decision)}：${text(message.data.reason)}`]
  } else if (message.event === 'report.done') {
    next.reportUrl = text(message.data.html_url)
  } else if (message.event === 'error') {
    next.errors = [...state.errors, text(message.data.message)]
  } else if (message.event === 'forum.message') {
    next.forum = [...state.forum, {
      agent: text(message.data.agent),
      type: text(message.data.type),
      content: text(message.data.content),
      round: typeof message.data.round === 'number' ? message.data.round : 0,
    }]
  } else if (message.event === 'host.review') {
    next.hostReviews = [...state.hostReviews, {
      release: message.data.release === true,
      reason: text(message.data.reason),
      gaps: Array.isArray(message.data.gaps) ? message.data.gaps as Array<{ desc?: string; priority?: string }> : [],
    }]
  } else if (message.event === 'budget.update') {
    next.budget = {
      tokensUsed: typeof message.data.tokens_used === 'number' ? message.data.tokens_used : 0,
      tokensLimit: typeof message.data.tokens_limit === 'number' ? message.data.tokens_limit : 0,
      calls: typeof message.data.calls === 'number' ? message.data.calls : 0,
    }
  } else if (message.event === 'search.result' && text(message.data.degraded_from)) {
    next.degradations = [...state.degradations, `已从 ${text(message.data.degraded_from)} 降级到 ${text(message.data.provider)}`]
  } else if (message.event === 'warning') {
    next.degradations = [...state.degradations, text(message.data.message)]
  }
  return next
}
