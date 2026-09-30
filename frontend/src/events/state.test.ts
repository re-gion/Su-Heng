import { describe, expect, it } from 'vitest'
import { applyEvent, initialStreamState, type TaskEvent } from './state'

function event(seq: number, name: string, data: Record<string, unknown>): TaskEvent {
  return { event: name, task_id: 't1', seq, ts: '2026-08-12T00:00:00+08:00', data }
}

describe('SSE public state seam', () => {
  it('tracks active and paused time from transitions before the first budget event', () => {
    const running = { ...event(1, 'task.status', { status: 'running', phase: 'forum' }), ts: '2026-09-29T00:00:00Z' }
    let state = applyEvent(initialStreamState, running)
    expect(state.timing?.active_seconds).toBe(0)
    state = applyEvent(state, { ...event(2, 'task.status', { status: 'paused', phase: 'forum' }), ts: '2026-09-29T00:25:00Z' })
    expect(state.timing?.active_seconds).toBe(1500)
    state = applyEvent(state, { ...event(3, 'task.status', { status: 'running', phase: 'verifying' }), ts: '2026-09-29T00:27:00Z' })
    expect(state.timing?.waiting_seconds).toBe(120)
    state = applyEvent(state, { ...event(4, 'task.status', { status: 'done', phase: 'finished' }), ts: '2026-09-29T00:28:00Z' })
    expect(state.timing?.active_seconds).toBe(1560)
  })

  it('counts finished verification records once and distinguishes incomplete work', () => {
    let state = applyEvent(initialStreamState, event(1, 'verify.progress', { claim_id: 'C1', verification_state: 'complete', total: 2 }))
    state = applyEvent(state, event(2, 'verify.progress', { claim_id: 'C1', verification_state: 'complete', total: 2 }))
    state = applyEvent(state, event(3, 'verify.progress', { claim_id: 'C2', verification_state: 'incomplete', total: 2 }))
    expect(state.verification).toEqual({ claims: { C1: 'complete', C2: 'incomplete' }, total: 2 })
    expect(applyEvent(state, event(3, 'verify.progress', {}))).toBe(state)
  })

  it('separates date recovery and actual rejection reasons for each attempt', () => {
    const state = applyEvent(initialStreamState, event(1, 'search.result', {
      provider: 'exa', hits: 5,
      provider_diagnostics: [
        { provider: 'langsearch', status: 'relevance_filtered_empty', count: 8, rejected: { subject_mismatch: 8 } },
        { provider: 'langsearch', status: 'relevance_filtered_empty', count: 8, recovery_reason: 'relax_preferred_date_window', rejected: { subject_mismatch: 5, event_mismatch: 1, foreign_source_in_domestic_phase: 2 } },
        { provider: 'exa', status: 'success', count: 8, accepted_count: 5 },
      ],
    }))
    expect(state.degradations[0]).toContain('主体不符 8 条')
    expect(state.degradations[0]).toContain('LangSearch（放宽日期补查）')
    expect(state.degradations[0]).toContain('非当前事件 1 条')
    expect(state.degradations[0]).toContain('境外来源不符合当前国内范围 2 条')
    expect(state.degradations[0]).not.toContain('调用失败')
  })

  it('explains an exhausted budget before the date recovery request', () => {
    const state = applyEvent(initialStreamState, event(1, 'search.result', {
      hits: 0,
      provider_diagnostics: [
        { provider: 'langsearch', status: 'budget_exhausted', recovery_reason: 'relax_preferred_date_window' },
      ],
    }))
    expect(state.degradations[0]).toContain('搜索调用预算已用完，本次未调用上游接口')
  })

  it('replays reservation and separate active/waiting timing diagnostics', () => {
    const timing = { available: true, active_seconds: 60, waiting_seconds: 120, phase_seconds: { reporting: 60 } }
    const state = applyEvent(initialStreamState, event(1, 'budget.update', {
      tokens_used: 500, tokens_limit: 1400000, calls: 8,
      tokens_reserved: 12000, phase_token_limit: 840000, timing,
    }))
    expect(state.budget).toEqual({ tokensUsed: 500, tokensLimit: 1400000, calls: 8, tokensReserved: 12000, phaseTokenLimit: 840000, timing })
    expect(applyEvent(state, event(1, 'budget.update', { calls: 1 }))).toBe(state)
  })
  it('renders agent, evidence and loop events once in sequence', () => {
    const agent = applyEvent(initialStreamState, event(1, 'agent.status', { phase: 'searching' }))
    const evidence = applyEvent(agent, event(2, 'evidence.added', { evidence_id: 'E001', title: '通报', source_name: '监管机构', source_tier: 1 }))
    const decision = applyEvent(evidence, event(3, 'loop.round', { decision: 'stop', reason: '零增益' }))
    const duplicate = applyEvent(decision, event(2, 'evidence.added', { evidence_id: 'E001', title: '重复' }))

    expect(duplicate.agentPhase).toBe('searching')
    expect(duplicate.evidence).toHaveLength(1)
    expect(duplicate.decisions).toEqual(['结束本轮调查：零增益'])
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

  it('keeps directed forum metadata and separate recovery summaries through replay', () => {
    let state = applyEvent(initialStreamState, event(1, 'forum.message', {
      agent: 'moderator', type: 'directive', content: '核对原文', round: 2,
      payload: { agent: 'fact_investigator' },
    }))
    state = applyEvent(state, event(2, 'forum.message', {
      agent: 'history_insight', type: 'summary', content: '补得一条事实', round: 3,
      refs: ['E012'],
      payload: { phase: 'quality_recovery', summary_items: [{ text: '补得一条事实', claim_ref: 'C007', evidence_refs: ['E012'] }] },
    }))
    const replay = applyEvent(state, event(2, 'forum.message', { agent: 'history_insight', type: 'summary', content: '重复', round: 3 }))

    expect(replay).toBe(state)
    expect(state.forum).toHaveLength(2)
    expect(state.forum[0].targetAgent).toBe('fact_investigator')
    expect(state.forum[1]).toMatchObject({ phase: 'quality_recovery', refs: ['E012'], summaryItems: [{ text: '补得一条事实', claimRef: 'C007', evidenceRefs: ['E012'] }] })
  })

  it('describes relevance fallback without claiming the provider failed', () => {
    const state = applyEvent(initialStreamState, event(1, 'search.result', {
      provider: 'qianfan',
      degraded_from: 'langsearch',
      hits: 8,
      provider_diagnostics: [
        { provider: 'langsearch', status: 'relevance_filtered_empty', count: 8, accepted_count: 0 },
        { provider: 'qianfan', status: 'success', count: 8, accepted_count: 8 },
      ],
    }))

    expect(state.degradations).toEqual([
      'LangSearch 返回 8 条，但均未通过事件相关性筛选；千帆获得 8 条符合事件范围的材料。',
    ])
    expect(state.degradations[0]).not.toContain('降级')
  })

  it('labels historical comparison hits as preliminary leads', () => {
    const state = applyEvent(initialStreamState, event(1, 'search.result', {
      agent: 'history_insight',
      provider: 'qianfan',
      hits: 1,
      provider_diagnostics: [
        { provider: 'langsearch', status: 'relevance_filtered_empty', count: 8, accepted_count: 0 },
        { provider: 'exa', status: 'relevance_filtered_empty', count: 8, accepted_count: 0 },
        { provider: 'qianfan', status: 'success', count: 8, accepted_count: 1 },
      ],
    }))

    expect(state.degradations).toEqual([
      'LangSearch 返回 8 条，但均未通过历史对照初筛；Exa 返回 8 条，但均未通过历史对照初筛；千帆有 1 条历史对照候选材料通过初筛，仍需核验来源。',
    ])
  })

  it('explains why Exa supplemented a one-source LangSearch result', () => {
    const state = applyEvent(initialStreamState, event(1, 'search.result', {
      provider: 'exa',
      continued_from: 'langsearch',
      hits: 8,
      provider_diagnostics: [
        { provider: 'langsearch', status: 'insufficient_coverage', count: 8, accepted_count: 2, source_groups: 1 },
        { provider: 'exa', status: 'success', count: 8, accepted_count: 8, returned_count: 8 },
      ],
    }))

    expect(state.degradations).toEqual([
      'LangSearch 筛选后保留 2 条，但只覆盖 1 个候选发布主体，尝试用 Exa 补充；Exa 获得 8 条符合事件范围的材料。',
    ])
  })

  it('distinguishes a local quota guard from an invalid provider key', () => {
    const state = applyEvent(initialStreamState, event(1, 'search.result', {
      provider: 'bocha',
      degraded_from: 'langsearch',
      hits: 1,
      provider_diagnostics: [
        { provider: 'langsearch', status: 'relevance_filtered_empty', count: 8, accepted_count: 0 },
        // Historical events recorded the exception type before the structured reason existed.
        { provider: 'qianfan', status: 'error', error_type: 'ProviderQuotaExceeded' },
        { provider: 'bocha', status: 'success', count: 8, accepted_count: 1 },
      ],
    }))

    expect(state.degradations).toEqual([
      'LangSearch 返回 8 条，但均未通过事件相关性筛选；千帆达到本地额度保护线，本次未调用上游接口；博查获得 1 条符合事件范围的材料。',
    ])
    expect(state.degradations[0]).not.toContain('Key')
  })

  it('reports that all foreign providers produced no relevant material', () => {
    const state = applyEvent(initialStreamState, event(1, 'search.result', {
      provider: 'serper',
      degraded_from: 'exa',
      hits: 0,
      provider_diagnostics: [
        { provider: 'exa', status: 'relevance_filtered_empty', count: 8, accepted_count: 0 },
        { provider: 'tavily', status: 'relevance_filtered_empty', count: 8, accepted_count: 0 },
        { provider: 'serper', status: 'empty', count: 0, accepted_count: 0 },
      ],
    }))

    expect(state.degradations).toEqual([
      'Exa 返回 8 条，但均未通过事件相关性筛选；Tavily 返回 8 条，但均未通过事件相关性筛选；Serper 未返回结果；本次查询未找到符合事件范围的材料。',
    ])
    expect(state.degradations[0]).not.toContain('降级到')
  })
})
