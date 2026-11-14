// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AgentSkillRoster, AgentSkillSummary } from './AgentSkills'
import { agentCatalogFixture } from '../testFixtures/agentCatalog'
import type { SkillActivity } from '../events/state'

beforeEach(() => vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(agentCatalogFixture)))))
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('actual Agent methods and activity', () => {
  it('shows eight roles and keeps disabled comments explicit without inventing usage', async () => {
    render(<AgentSkillRoster phases={{}} finished />)
    expect(await screen.findByRole('heading', { name: '任务准备 Agent' })).toBeInTheDocument()
    expect(screen.getAllByRole('heading', { level: 3 })).toHaveLength(8)
    const comments = screen.getByRole('heading', { name: '评论洞察 Agent' }).closest('article')!
    expect(within(comments).getByText('未启用')).toBeInTheDocument()
    expect(screen.getAllByText('本次未参与')).toHaveLength(7)
    expect(screen.getByText(/旧任务不会补造使用历史/)).toBeInTheDocument()
  })

  it('shows versioned read-only methods and actual stop conditions', () => {
    render(<AgentSkillSummary agent={agentCatalogFixture.agents[0]} />)
    fireEvent.click(screen.getByText('查看 1 项专项与共享技能'))
    expect(screen.getByText(/停止：材料不足保留缺口/)).toBeVisible()
    expect(screen.getByText(/技能内容只读/)).toBeVisible()
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })

  it('keeps response return distinct from saved stage artifacts and filters activity', async () => {
    const activity: SkillActivity = {
      invocation_id: 'run1', agent: 'fact_investigator', role: 'analyst_a', skill_id: 'fact-method',
      label: '冲突陈述对照', version: '1.0.0', digest: 'abc', stage: 'reflect', status: 'response_ready',
      selection_mode: 'model', reason: '核对批次范围差异。', outcome: '响应返回，待业务处理。',
      claim_refs: [], evidence_refs: [], report_id: '', at: '',
    }
    const view = render(<AgentSkillRoster phases={{}} activities={[activity]} />)
    await screen.findByRole('heading', { name: '事实调查 Agent' })
    expect(screen.getAllByText(/响应已返回，待业务处理/).length).toBeGreaterThan(0)
    view.rerender(<AgentSkillRoster phases={{}} activities={[{ ...activity, status: 'stage_partial', evidence_refs: ['E001'], outcome: '本轮仅保存一份材料，缺口仍在。' }]} />)
    fireEvent.click(screen.getByText('冲突陈述对照', { selector: '.skill-activity summary strong' }))
    expect(screen.getByText('本轮仅保存一份材料，缺口仍在。')).toBeVisible()
    expect(screen.getByRole('link', { name: 'E001' })).toHaveAttribute('href', '#evidence-E001')
    fireEvent.change(screen.getByLabelText('查看角色'), { target: { value: 'moderator' } })
    expect(screen.queryByText('本轮仅保存一份材料，缺口仍在。')).not.toBeInTheDocument()
  })
})
