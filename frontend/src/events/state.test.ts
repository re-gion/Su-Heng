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
})
