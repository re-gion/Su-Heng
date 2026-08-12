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
  evidence: EvidenceItem[]
  decisions: string[]
  reportUrl: string | null
  errors: string[]
}

export const initialStreamState: StreamState = {
  lastSeq: 0,
  status: 'queued',
  phase: 'planning',
  agentPhase: '等待启动',
  evidence: [],
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
  }
  return next
}
