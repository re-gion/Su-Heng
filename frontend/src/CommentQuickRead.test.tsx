// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { CommentQuickRead } from './CommentQuickRead'
import type { CommentQuestionData } from './api/client'

const data: CommentQuestionData = {
  block: { analysis_mode: 'quick_read', coverage: { scope_review_incomplete: 399 },
    samples: [{ id: 'm1', text: '认可处理，但请公开程序', platform: 'weibo', source_url: 'https://example.test/post' }, { id: 'm2', text: '希望核对依据', platform: 'weibo', source_url: 'https://example.test/post' }],
    observations: [{ id: 'o1', text: '要求公开程序', stance: '质疑', comment_refs: ['m1'] }, { id: 'o2', text: '核对依据', stance: '审慎', comment_refs: ['m2'] }],
    items: [{ id: 'q1', title: '程序如何公开？', summary: '样本希望了解程序', sample_count: 1, observation_refs: ['o1'], publicly_verifiable: true, comparisons: [], judgements: [] }] },
  jobs: {}, deepening_available: true,
}
beforeEach(() => vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(data)))))
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('does not start analysis on opening and preserves minority/raw browsing and pending scope', async () => {
  render(<CommentQuickRead taskId="t1" />)
  await screen.findByText('程序如何公开？')
  expect(vi.mocked(fetch).mock.calls.every(([, init]) => init?.method !== 'POST')).toBe(true)
  expect(screen.getByText(/399 条原始评论/)).toBeInTheDocument()
  expect(screen.getByText('深入分析未启动。')).toBeInTheDocument()
  expect(screen.getByText('已审关切与诉求，问题归纳待补充（1 条）')).toBeInTheDocument()
  expect(screen.queryByText('开启公开补查')).not.toBeInTheDocument()
  fireEvent.change(screen.getByPlaceholderText('输入关切或原话中的词'), { target: { value: '依据' } })
  expect(screen.getByText('1 条匹配样本')).toBeInTheDocument()
})

it('only explicit selection submits the requested existing-evidence mode', async () => {
  vi.mocked(fetch).mockImplementation(async (_url, init) => new Response(JSON.stringify(init?.method === 'POST' ? { status: 'queued' } : data)))
  render(<CommentQuickRead taskId="t1" />)
  await screen.findByText('程序如何公开？')
  fireEvent.click(screen.getByText('对照已有材料'))
  await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([url, init]) => String(url).endsWith('/q1/analyze') && init?.body === JSON.stringify({ mode: 'existing' }))).toBe(true))
})

it('shows reviewed concerns immediately when question grouping is incomplete', async () => {
  vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify({ ...data, block: { ...data.block, items: [] } })))
  render(<CommentQuickRead taskId="t1" />)
  const text = await screen.findByText('核对依据')
  expect(text.closest('details')?.open).toBe(true)
  expect(vi.mocked(fetch).mock.calls.every(([, init]) => init?.method !== 'POST')).toBe(true)
})
