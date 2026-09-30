// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

type Listener = (event: MessageEvent<string>) => void

class FakeEventSource {
  static instances: FakeEventSource[] = []
  listeners = new Map<string, Listener[]>()
  closed = false
  close = vi.fn(() => { this.closed = true })

  constructor(public url: string) {
    FakeEventSource.instances.push(this)
  }

  addEventListener(name: string, listener: EventListener) {
    const listeners = this.listeners.get(name) ?? []
    listeners.push(listener as Listener)
    this.listeners.set(name, listeners)
  }

  emit(name: string, data: Record<string, unknown>, seq = 1) {
    if (this.closed) return
    const payload = JSON.stringify({ event: name, task_id: 't1', seq, ts: '2026-08-13', data })
    for (const listener of this.listeners.get(name) ?? []) listener(new MessageEvent(name, { data: payload }))
  }
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

function baseFetch(input: RequestInfo | URL, init?: RequestInit) {
  const path = String(input)
  if (path === '/api/config') return Promise.resolve(json({ llm: { default: { api_key: null, base_url: '', model: '' }, roles: {} }, search: { provider_order: ['langsearch', 'qianfan', 'bocha', 'exa', 'tavily', 'serper'], keys: { langsearch: 'la-***key', qianfan: 'qi-***key', bocha: 'bo-***key', exa: 'ex-***key', tavily: 'ta-***key', serper: 'se-***key' }, quota: { exa: { state: 'metered_without_fixed_limit', upstream_quota_verified: false, windows: [{ period: 'month', period_key: 'month:2026-09', unit: 'milli_usd', used: 20024, normal_limit: null, critical_limit: null, normal_remaining: null, critical_remaining: null }] }, qianfan: { state: 'normal_limit_reached', upstream_quota_verified: false, windows: [{ period: 'day', period_key: 'day:2026-09-22', unit: 'calls', used: 40, normal_limit: 40, critical_limit: 50, normal_remaining: 0, critical_remaining: 10 }] } } }, fetch: { provider_order: ['builtin', 'firecrawl'], keys: { firecrawl: 'fc-***key' } }, comments: { enabled: false }, budget: { overrides: {}, effective: {}, defaults: {}, fields: [], depths: [], source: 'default' } }))
  if (path === '/api/comment-plugin/status') return Promise.resolve(json({ enabled: false, available: false, demo_mode: false, container: false, platforms: [], risk_notice: '' }))
  if (path.startsWith('/api/tasks?')) return Promise.resolve(json({ items: [] }))
  if (path === '/api/data/status') return Promise.resolve(json({ demo_mode: false, assets: 0, historical_events: 0, hot_snapshots: 0, hot_coverage: { from: null, to: null }, scheduler: {}, report_ttl_hours: null }))
  if (path === '/api/tasks/t1') return Promise.resolve(json({ task_id: 't1', event_query: '测试任务', status: 'paused', phase: 'comment_selection', resumable: false, comment_selection_required: true, report_id: null }))
  if (path === '/api/tasks/t-empty') return Promise.resolve(json({ task_id: 't-empty', event_query: '空候选任务', status: 'paused', phase: 'comment_selection', resumable: false, comment_selection_required: true, report_id: null }))
  if (path === '/api/tasks/t-stale') return Promise.resolve(json({ task_id: 't-stale', event_query: '陈旧任务', status: 'done', phase: 'finished', resumable: false, comment_selection_required: false, report_id: 'r1', release_label: 'full_report', recovery: { end_reason: 'core_complete' } }))
  if (path === '/api/tasks/t-topic') return Promise.resolve(json({ task_id: 't-topic', event_query: '武汉大学舆情', resolved_event_query: null, status: 'paused', phase: 'topic_selection', resumable: false, topic_selection_required: true, comment_selection_required: false, report_id: null }))
  if (path === '/api/tasks/t-topic/topic-candidates') return Promise.resolve(json({ task_id: 't-topic', phase: 'topic_selection', original_query: '武汉大学舆情', manual_entry_allowed: true, attempts: [], provider_coverage: { configured: 2, attempted: ['a', 'b'], successful: ['a', 'b'], limited: false }, effective_time_range: { date_from: '2025-01-01', date_to: '2026-01-01' }, used_default_time_range: false, manual_preflight: null, items: [{ id: 'tc1', title: '武汉大学图书馆事件及校方回应', query: '武汉大学图书馆事件及校方回应', summary: '校方回应图书馆相关争议。', confidence: 'confirmed', confidence_label: '已证实事件候选', score: 82, reasons: ['含官方或当事方一手来源'], gaps: [], sources: [{ url: 'https://www.whu.edu.cn/example', title: '情况说明', source_name: '武汉大学', published_at: '2025-09-20T08:00:00+08:00', role: 'party', provider: 'a' }], source_count: 1, date_from: '2025-09-20', date_to: '2025-09-20', coverage_limited: false, source_name: '武汉大学', url: 'https://www.whu.edu.cn/example', published_at: '2025-09-20T08:00:00+08:00', date_status: '范围内' }] }))
  if (path === '/api/tasks/t-topic-empty') return Promise.resolve(json({ task_id: 't-topic-empty', event_query: '武汉大学舆情', resolved_event_query: null, status: 'paused', phase: 'topic_selection', resumable: false, topic_selection_required: true, comment_selection_required: false, report_id: null }))
  if (path === '/api/tasks/t-topic-empty/topic-candidates') return Promise.resolve(json({ task_id: 't-topic-empty', phase: 'topic_selection', original_query: '武汉大学舆情', manual_entry_allowed: true, attempts: [{ round: 'initial', query: '武汉大学 通报 回应', language: 'zh', provider: 'only', status: 'empty', raw_hits: 10, accepted_hits: 0, rejected: { generic_or_routine: 10 } }], provider_coverage: { configured: 1, attempted: ['only'], successful: [], limited: true, message: '当前没有形成有效检索路径。' }, effective_time_range: { date_from: '2025-09-21', date_to: '2026-09-21' }, used_default_time_range: true, manual_preflight: null, items: [] }))
  if (path === '/api/tasks/t-topic/topic-selection' && init?.method === 'POST') return Promise.resolve(json({ status: 'running', resolved_event_query: '武汉大学图书馆事件及校方回应' }))
  if (path === '/api/tasks/t1/comment-candidates') return Promise.resolve(json({ task_id: 't1', phase: 'comment_selection', budgets: { posts: 2, comments_per_post: 100 }, discovery_attempts: [], manual_entry_allowed: true, items: [{ id: 'sc1', url: 'https://weibo.com/123/AbCd', platform: 'weibo', title: '高价值候选', snippet: '事件评论', score: 78.5, score_breakdown: { relevance: 30 }, reasons: ['相关性高'], selection_mode: 'smart', status: 'pending', login_profile_present: false }] }))
  if (path === '/api/tasks/t-empty/comment-candidates') return Promise.resolve(json({ task_id: 't-empty', phase: 'comment_selection', budgets: { posts: 2, comments_per_post: 100 }, discovery_attempts: [{ platform: 'weibo', status: 'failed', count: 0, error: 'TimeoutError' }, { platform: 'bilibili', status: 'empty', count: 0 }], manual_entry_allowed: true, items: [] }))
  if ((path === '/api/tasks/t1/comment-selection' || path === '/api/tasks/t-empty/comment-selection') && init?.method === 'POST') {
    const payload = JSON.parse(String(init.body)) as { action: string }
    return Promise.resolve(payload.action === 'approve' && path === '/api/tasks/t1/comment-selection' ? json({ error: { message: '微博尚未登录' } }, 409) : json({ status: 'running' }))
  }
  if (path === '/api/tasks/t-stale/stop' && init?.method === 'POST') return Promise.resolve(json({ error: { message: '不该对已完成任务发 stop' } }, 409))
  if (path === '/api/tasks/t-stale/comment-selection' && init?.method === 'POST') return Promise.resolve(json({ error: { message: '不该对已完成任务发 comment-selection' } }, 409))
  throw new Error(`unexpected fetch: ${path}`)
}

describe('V2 task desk', () => {
  beforeEach(() => {
    history.replaceState(null, '', '/')
    FakeEventSource.instances = []
    vi.stubGlobal('EventSource', FakeEventSource)
    vi.stubGlobal('fetch', vi.fn(baseFetch))
    vi.stubGlobal('scrollTo', vi.fn())
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('keeps the comment seat hidden when disabled and reveals manual URL input on demand', async () => {
    render(<App />)
    await screen.findByRole('heading', { name: '舆情调查与研判' })
    expect(screen.queryByText('本地辅助层')).not.toBeInTheDocument()
    expect(vi.mocked(fetch).mock.calls.some(([url]) => url === '/api/data/status')).toBe(false)
    expect(document.querySelector('[data-agent="comment_insight"]')).not.toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('评论深挖'), { target: { value: 'manual' } })

    expect(screen.getByLabelText('指定帖子 URL')).toBeInTheDocument()
  })

  it('shows the fixed search policy without reorder controls', async () => {
    render(<App />)
    fireEvent.click(screen.getByRole('button', { name: '返回顶部' }))
    expect(window.scrollTo).toHaveBeenCalledWith({ top: 0, behavior: 'smooth' })
    fireEvent.click(await screen.findByRole('button', { name: '系统配置 ↗' }))

    expect(await screen.findByText('搜索路由与本地额度')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '返回顶部' }))
    expect(window.scrollTo).toHaveBeenCalledTimes(2)
    expect(await screen.findByText(/今日已记录 40\/40 次 · 常规保护线已到达/)).toBeInTheDocument()
    expect(screen.getByText('本地保护：本月已记录 20024 毫美元')).toBeInTheDocument()
    expect(screen.queryByText(/20024.*常规余量/)).not.toBeInTheDocument()
    expect(screen.getByText(/文档参考 50 次\/日、1500 次\/月/)).toBeInTheDocument()
    expect(screen.getByText(/本地保护只按免费试用包计算/)).toBeInTheDocument()
    expect(screen.getByText(/本机计数从此版本启用后开始记录/)).toBeInTheDocument()
    expect(screen.getByText(/LangSearch → Exa → 千帆 → 博查/)).toBeInTheDocument()
    expect(screen.getByText(/只找到同一发布主体的材料/)).toBeInTheDocument()
    expect(screen.getByText(/千帆与 Tavily 每任务最多 20 次，博查与 Serper 每任务最多 10 次/)).toBeInTheDocument()
    expect(screen.queryByLabelText('搜索优先级')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '↑' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '↓' })).not.toBeInTheDocument()
  })

  it('pauses a broad topic until a concrete event is selected', async () => {
    history.replaceState(null, '', '/?task=t-topic')
    render(<App />)

    expect(await screen.findByText('先选定这次要深入调查的事件')).toBeInTheDocument()
    expect(screen.getByText('武汉大学图书馆事件及校方回应')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '验证事件来源' })).toBeDisabled()
    fireEvent.click(screen.getByRole('radio'))
    fireEvent.click(screen.getByRole('button', { name: '确认事件并开始深入调查' }))

    await waitFor(() => {
      const call = vi.mocked(fetch).mock.calls.find(([url]) => String(url) === '/api/tasks/t-topic/topic-selection')
      expect(call).toBeTruthy()
      expect(JSON.parse(String(call?.[1]?.body)).candidate_id).toBe('tc1')
    })
  })

  it('explains empty topic discovery instead of silently showing a blank selector', async () => {
    history.replaceState(null, '', '/?task=t-topic-empty')
    render(<App />)

    expect(await screen.findByText(/没有用导航页、例行通知或无关结果凑数/)).toBeInTheDocument()
    expect(screen.getByLabelText('具体事件')).toBeInTheDocument()
  })

  it('renders confirmation candidates and the fourth seat, then surfaces missing login and allows skip', async () => {
    history.replaceState(null, '', '/?task=t1')
    render(<App />)
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))

    act(() => FakeEventSource.instances[0].emit('task.status', { status: 'paused', phase: 'comment_selection', progress: 68 }))

    expect(await screen.findByText(/高价值候选/)).toBeInTheDocument()
    expect(document.querySelector('[data-agent="comment_insight"]')).toBeInTheDocument()
    expect(screen.getByText(/需先登录/)).toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: '补充帖子 URL' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '确认并采集' }))
    expect(await screen.findByText('微博尚未登录')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '跳过评论，继续报告' }))
    await waitFor(() => {
      const calls = vi.mocked(fetch).mock.calls.filter(([url]) => String(url) === '/api/tasks/t1/comment-selection')
      expect(calls.some(([, init]) => JSON.parse(String(init?.body)).action === 'skip')).toBe(true)
    })
  })

  it('hides stale comment-selection actions after loading authoritative task detail', async () => {
    history.replaceState(null, '', '/?task=t-stale')
    render(<App />)
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))

    act(() => FakeEventSource.instances[0].emit('task.status', { status: 'paused', phase: 'comment_selection', progress: 68 }))
    expect(FakeEventSource.instances[0].close).not.toHaveBeenCalled()
    act(() => FakeEventSource.instances[0].emit('task.status', { status: 'running', phase: 'reporting', progress: 88 }, 2))
    act(() => FakeEventSource.instances[0].emit('report.done', { html_url: '/api/reports/r1/html?view=full' }, 3))
    act(() => FakeEventSource.instances[0].emit('task.status', { status: 'done', phase: 'finished', progress: 100 }, 4))

    await waitFor(() => {
      expect(screen.queryByText('选择值得进入的评论区')).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: '停止并出报告' })).not.toBeInTheDocument()
      expect(screen.getByRole('link', { name: '查看调查报告 →' })).toBeInTheDocument()
    })

    const forbiddenCalls = vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes('/api/tasks/t-stale/comment-selection') || String(url).includes('/api/tasks/t-stale/stop'))
    expect(forbiddenCalls).toHaveLength(0)
    expect(FakeEventSource.instances[0].close).toHaveBeenCalledTimes(1)
  })

  it('shows directed instructions, readable summaries and a separate recovery phase', async () => {
    history.replaceState(null, '', '/?task=t-stale')
    render(<App />)
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
    const source = FakeEventSource.instances[0]

    act(() => {
      source.emit('forum.message', { agent: 'fact_investigator', type: 'summary', content: '核实起因；核实处理结果。', round: 1 }, 1)
      source.emit('forum.message', { agent: 'moderator', type: 'directive', content: '补查公告', round: 1, payload: { agent: 'fact_investigator' } }, 2)
      source.emit('forum.message', { agent: 'history_insight', type: 'summary', content: '核实前例', round: 2, payload: { phase: 'quality_recovery', summary_items: [{ text: '核实前例', claim_ref: 'C008', evidence_refs: ['E011'] }] } }, 3)
      source.emit('agent.status', { agent: 'history_insight', phase: 'quality_recovery' }, 4)
    })

    expect(screen.getByText('第 1 轮 · 主持人 → 事实调查')).toBeInTheDocument()
    expect(screen.getByText('核实起因；')).toBeInTheDocument()
    expect(screen.getByText('核实处理结果。')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '核验后专项补查' })).toBeInTheDocument()
    expect(screen.getByText('核验后专项补查 · 历史洞察')).toBeInTheDocument()
    expect(screen.getByText('C008 · E011')).toBeInTheDocument()
    expect(screen.queryByText('第 2 轮 · 历史洞察')).not.toBeInTheDocument()
    expect(await screen.findByText(/报告等级：完整舆情专报/)).toBeInTheDocument()
  })

  it('confirms institution scope before creating a mixed public-interest task', async () => {
    const requests: Record<string, unknown>[] = []
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === '/api/tasks' && init?.method === 'POST') {
        const payload = JSON.parse(String(init.body)) as Record<string, unknown>
        requests.push(payload)
        return Promise.resolve(payload.investigation_scope === 'public_event'
          ? json({ task_id: 't-scoped', status: 'queued', events_url: '/api/tasks/t-scoped/events', created_at: '2026-09-27' }, 202)
          : json({ error: { code: 'SCOPE_CONFIRMATION_REQUIRED', message: '请确认范围', details: { scope_label: '仅调查校方公开回应与处理。' } } }, 409))
      }
      if (String(input) === '/api/tasks/t-scoped') return Promise.resolve(json({ task_id: 't-scoped', event_query: '某高校图书馆事件', status: 'done', phase: 'finished', report_id: null }))
      return baseFetch(input, init)
    }))
    render(<App />)
    fireEvent.change(screen.getByLabelText('事件名称'), { target: { value: '某高校图书馆事件' } })
    fireEvent.click(screen.getByRole('button', { name: '启动三路调查' }))

    expect(await screen.findByText('确认调查范围')).toBeInTheDocument()
    expect(screen.getByText('仅调查校方公开回应与处理。')).toBeInTheDocument()
    expect(requests).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: '按公开事件范围继续' }))
    await waitFor(() => expect(requests).toHaveLength(2))
    expect(requests[0].investigation_scope).toBe('general')
    expect(requests[1].investigation_scope).toBe('public_event')
  })

  it('shows an empty state when smart selection finds no candidates', async () => {
    history.replaceState(null, '', '/?task=t-empty')
    render(<App />)
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))

    act(() => FakeEventSource.instances[0].emit('task.status', { status: 'paused', phase: 'comment_selection', progress: 68 }))

    expect(await screen.findByText(/系统没有找到达到相关度门槛的帖子/)).toBeInTheDocument()
    expect(screen.getByText('发现失败', { exact: false })).toBeInTheDocument()
    expect(screen.getByText('· TimeoutError', { exact: false })).toBeInTheDocument()
    expect(screen.getByLabelText('补充帖子 URL')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '明确跳过评论，继续报告' })).toBeEnabled()
  })
})
