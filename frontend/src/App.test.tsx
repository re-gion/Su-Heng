// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

type Listener = (event: MessageEvent<string>) => void

class FakeEventSource {
  static instances: FakeEventSource[] = []
  listeners = new Map<string, Listener[]>()
  close = vi.fn()

  constructor(public url: string) {
    FakeEventSource.instances.push(this)
  }

  addEventListener(name: string, listener: EventListener) {
    const listeners = this.listeners.get(name) ?? []
    listeners.push(listener as Listener)
    this.listeners.set(name, listeners)
  }

  emit(name: string, data: Record<string, unknown>, seq = 1) {
    const payload = JSON.stringify({ event: name, task_id: 't1', seq, ts: '2026-08-13', data })
    for (const listener of this.listeners.get(name) ?? []) listener(new MessageEvent(name, { data: payload }))
  }
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

function baseFetch(input: RequestInfo | URL, init?: RequestInit) {
  const path = String(input)
  if (path.startsWith('/api/tasks?')) return Promise.resolve(json({ items: [] }))
  if (path === '/api/data/status') return Promise.resolve(json({ demo_mode: false, assets: 0, historical_events: 0, hot_snapshots: 0, hot_coverage: { from: null, to: null }, scheduler: {}, report_ttl_hours: null }))
  if (path === '/api/tasks/t1') return Promise.resolve(json({ task_id: 't1', event_query: '测试任务', status: 'paused', phase: 'comment_selection', resumable: false, comment_selection_required: true, report_id: null }))
  if (path === '/api/tasks/t-empty') return Promise.resolve(json({ task_id: 't-empty', event_query: '空候选任务', status: 'paused', phase: 'comment_selection', resumable: false, comment_selection_required: true, report_id: null }))
  if (path === '/api/tasks/t-stale') return Promise.resolve(json({ task_id: 't-stale', event_query: '陈旧任务', status: 'done', phase: 'finished', resumable: false, comment_selection_required: false, report_id: 'r1' }))
  if (path === '/api/tasks/t1/comment-candidates') return Promise.resolve(json({ task_id: 't1', phase: 'comment_selection', budgets: { posts: 2, comments_per_post: 100 }, items: [{ id: 'sc1', url: 'https://weibo.com/123/AbCd', platform: 'weibo', title: '高价值候选', snippet: '事件评论', score: 78.5, score_breakdown: { relevance: 30 }, reasons: ['相关性高'], selection_mode: 'smart', status: 'pending', login_profile_present: false }] }))
  if (path === '/api/tasks/t-empty/comment-candidates') return Promise.resolve(json({ task_id: 't-empty', phase: 'comment_selection', budgets: { posts: 2, comments_per_post: 100 }, items: [] }))
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
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('keeps the comment seat hidden when disabled and reveals manual URL input on demand', async () => {
    render(<App />)
    await screen.findByText('本地辅助层')
    expect(document.querySelector('[data-agent="comment_insight"]')).not.toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('评论深挖'), { target: { value: 'manual' } })

    expect(screen.getByLabelText('指定帖子 URL')).toBeInTheDocument()
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
    act(() => FakeEventSource.instances[0].emit('report.done', { html_url: '/api/reports/r1/html?view=full' }, 2))

    await waitFor(() => {
      expect(screen.queryByText('选择值得进入的评论区')).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: '停止并出报告' })).not.toBeInTheDocument()
      expect(screen.getByRole('link', { name: '打开完整专报 →' })).toBeInTheDocument()
    })

    const forbiddenCalls = vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes('/api/tasks/t-stale/comment-selection') || String(url).includes('/api/tasks/t-stale/stop'))
    expect(forbiddenCalls).toHaveLength(0)
  })

  it('shows an empty state when smart selection finds no candidates', async () => {
    history.replaceState(null, '', '/?task=t-empty')
    render(<App />)
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))

    act(() => FakeEventSource.instances[0].emit('task.status', { status: 'paused', phase: 'comment_selection', progress: 68 }))

    expect(await screen.findByText('当前没有可审批的系统候选。你可以直接跳过评论继续报告，或返回调整调查范围后重试。')).toBeInTheDocument()
    expect(screen.queryByLabelText('补充帖子 URL')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '跳过评论，继续报告' })).toBeEnabled()
  })
})