import { describe, expect, it } from 'vitest'
import { applyEvent, initialStreamState, type TaskEvent } from './state'

function event(seq: number, name: string, data: Record<string, unknown>): TaskEvent {
  return { event: name, task_id: 't1', seq, ts: '2026-08-12T00:00:00+08:00', data }
}

describe('SSE public state seam', () => {
  it('renders agent, evidence and loop events once in sequence', () => {
    const agent = applyEvent(initialStreamState, event(1, 'agent.status', { phase: 'searching' }))
    const evidence = applyEvent(agent, event(2, 'evidence.added', { evidence_id: 'E001', title: '通报', source_name: '监管机构', source_tier: 1 }))
    const decision = applyEvent(evidence, event(3, 'loop.round', { decision: 'stop', reason: '零增益' }))
    const duplicate = applyEvent(decision, event(2, 'evidence.added', { evidence_id: 'E001', title: '重复' }))

    expect(duplicate.agentPhase).toBe('searching')
    expect(duplicate.evidence).toHaveLength(1)
    expect(duplicate.decisions).toEqual(['stop：零增益'])
    expect(duplicate.lastSeq).toBe(3)

    const reset = applyEvent(duplicate, event(0, '__reset__', {}))
    expect(reset).toEqual(initialStreamState)
  })

  it('renders forum host budget and independent agent phases from V1 events', () => {
    let state = initialStreamState
    state = applyEvent(state, event(1, 'agent.status', { agent: 'history_insight', phase: 'searching' }))
    state = applyEvent(state, event(2, 'forum.message', { agent: 'history_insight', type: 'summary', content: '找到历史对照', round: 1 }))
    state = applyEvent(state, event(3, 'host.review', { release: false, reason: '仍有一个缺口', gaps: [{ desc: '补查官方通报', priority: 'high' }] }))
    state = applyEvent(state, event(4, 'budget.update', { tokens_used: 1200, tokens_limit: 500000, calls: 3 }))

    expect(state.agents.history_insight).toBe('searching')
    expect(state.forum[0].content).toBe('找到历史对照')
    expect(state.hostReviews[0].reason).toBe('仍有一个缺口')
    expect(state.budget.tokensUsed).toBe(1200)
  })
})
