// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AgentSkillRoster, AgentSkillSummary } from './AgentSkills'
import { agentCatalogFixture } from '../testFixtures/agentCatalog'
import type { SkillActivity } from '../events/state'

const reporterLabel = agentCatalogFixture.agents.find(agent => agent.role === 'reporter')!.label

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
    expect(screen.getByText(/内容只读/)).toBeVisible()
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
    expect(screen.getAllByText(/响应已返回，采纳未确认/).length).toBeGreaterThan(0)
    view.rerender(<AgentSkillRoster phases={{}} activities={[{ ...activity, status: 'stage_partial', evidence_refs: ['E001'], outcome: '本轮仅保存一份材料，缺口仍在。' }]} />)
    fireEvent.click(screen.getByText('检查调查缺口', { selector: '.skill-activity summary strong' }))
    expect(screen.getByText('本轮仅保存一份材料，缺口仍在。')).toBeVisible()
    expect(screen.getByRole('link', { name: 'E001' })).toHaveAttribute('href', '#evidence-E001')
    fireEvent.change(screen.getByLabelText('查看角色'), { target: { value: 'moderator' } })
    expect(screen.queryByText('本轮仅保存一份材料，缺口仍在。')).not.toBeInTheDocument()
  })

  it('separates task totals from filtered records and groups repeated use of the same skill', async () => {
    const records = Array.from({ length: 19 }, (_, index): SkillActivity => ({
      invocation_id: `record-${index}`, agent: index < 5 ? 'reporter' : 'fact_investigator',
      role: index < 5 ? 'reporter' : 'analyst_a', skill_id: index < 5 ? 'evidence-bound-report' : 'fact-chain-reconstruction',
      label: index < 5 ? '证据约束的专报综合' : '事件事实链重建', version: '1.0.0', digest: 'same-file',
      stage: index === 0 ? 'report_summary' : index < 5 ? 'report_draft' : 'plan', status: 'stage_saved',
      selection_mode: index === 5 ? 'model' : 'work_order', reason: '根据当前工作单加载该角色的专项方法。',
      outcome: '', claim_refs: [], evidence_refs: [], report_id: '', at: '',
    }))
    render(<AgentSkillRoster phases={{}} activities={records} />)
    await screen.findByRole('heading', { name: reporterLabel })
    expect(screen.getByRole('status')).toHaveTextContent('19 条处理记录 · 2 项专项技能 · 1 条模型选用记录')
    fireEvent.change(screen.getByLabelText('查看角色'), { target: { value: 'reporter' } })
    expect(screen.getByText('技能使用记录 · 全任务 19 条处理记录')).toBeVisible()
    expect(screen.getByRole('status')).toHaveTextContent(`${reporterLabel} · 5 条处理记录 · 1 项专项技能 · 0 条模型选用记录`)
    expect(document.querySelectorAll('.skill-group')).toHaveLength(1)
    expect(screen.getByText(`${reporterLabel} · 5 条处理记录 · v1.0.0`)).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: '展开更早的 2 条处理记录' }))
    fireEvent.click(screen.getByText('生成或修订报告摘要', { selector: '.skill-activity summary strong' }))
    expect(screen.getByText(/程序根据“生成或修订报告摘要”工作阶段加载此技能/)).toBeVisible()
    expect(screen.queryByText('根据当前工作单加载该角色的专项方法。')).not.toBeInTheDocument()
    expect(screen.getByText('report_summary', { selector: '.skill-trace dd' })).not.toBeVisible()
    const record = screen.getByText('生成或修订报告摘要', { selector: '.skill-activity summary strong' }).closest<HTMLElement>('.skill-activity')!
    fireEvent.click(within(record).getByText('查看追溯信息'))
    expect(within(record).getByText('record-0', { selector: 'dd' })).toBeVisible()
    expect(within(record).getByText(/不是报告指纹/)).toBeVisible()
    expect(within(record).getByText(/同一记录更新状态或刷新回放时保持不变/)).toBeVisible()
  })

  it('keeps failures visible in a collapsed group and never calls program matching a model choice', async () => {
    const item: SkillActivity = {
      invocation_id: 'failed-1', agent: 'reporter', role: 'reporter', skill_id: 'report-method',
      label: '专报综合', version: '1.0.0', digest: 'file-1', stage: 'report_draft', status: 'failed',
      selection_mode: 'work_order', reason: '通用加载说明', outcome: '上游服务未返回合格结果。',
      claim_refs: [], evidence_refs: [], report_id: '', at: '',
    }
    render(<AgentSkillRoster phases={{}} activities={[item]} finished />)
    await screen.findByRole('heading', { name: reporterLabel })
    expect(screen.getByText('含未完成请求')).toBeVisible()
    expect(screen.getByRole('status')).toHaveTextContent('0 条模型选用记录')
    fireEvent.click(screen.getByText('生成报告草稿', { selector: '.skill-activity summary strong' }))
    expect(screen.getByText('上游服务未返回合格结果。')).toBeVisible()
    expect(screen.getByText(/本条没有进行模型选用/)).toBeVisible()
    expect(screen.queryByText('任务依据：')).not.toBeInTheDocument()
  })
})
