// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { TaskProgress } from './TaskProgress'
import { applyEvent, initialStreamState } from './state'

const start = '2026-09-29T00:00:00Z'
function stream(status = 'running', seq = 1, ts = start, previous = initialStreamState) {
  return { ...applyEvent(previous, { event: 'task.status', task_id: 't1', seq, ts, data: { status, phase: 'forum' } }), connection: 'connected' }
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date(start))
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals() })

it('ticks for 25 minutes without budget events, counts pause separately and freezes on completion', async () => {
  let value = stream()
  const view = render(<TaskProgress taskId="t1" stream={value} />)
  await act(async () => { await vi.advanceTimersByTimeAsync(25 * 60000) })
  expect(screen.getByText('主动运行 25 分 00 秒 · 用户等待 0 分 00 秒')).toBeInTheDocument()
  value = stream('paused', 2, '2026-09-29T00:25:00Z', value)
  view.rerender(<TaskProgress taskId="t1" stream={value} />)
  await act(async () => { await vi.advanceTimersByTimeAsync(60000) })
  expect(screen.getByText('主动运行 25 分 00 秒 · 用户等待 1 分 00 秒')).toBeInTheDocument()
  value = stream('done', 3, '2026-09-29T00:26:00Z', value)
  view.rerender(<TaskProgress taskId="t1" stream={value} />)
  await act(async () => { await vi.advanceTimersByTimeAsync(60000) })
  expect(screen.getByText('主动运行 25 分 00 秒 · 用户等待 1 分 00 秒')).toBeInTheDocument()
})

it('ignores stale snapshot state and corrects client clock skew', async () => {
  vi.setSystemTime(new Date('2026-09-29T00:05:00Z'))
  vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify({
    task_id: 't1', seq: 1, status: 'running', phase: 'forum',
    timing: { available: true, active_seconds: 0, waiting_seconds: 0, phase_seconds: {}, sampled_at: start, status: 'running' },
    verification: { total: 0 }, last_event_at: start,
  })))
  const value = stream()
  const view = render(<TaskProgress taskId="t1" stream={value} />)
  await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
  expect(screen.getByText('主动运行 0 分 01 秒 · 用户等待 0 分 00 秒')).toBeInTheDocument()
  view.rerender(<TaskProgress taskId="t1" stream={stream('paused', 2, '2026-09-29T00:00:01Z', value)} />)
  await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
  expect(screen.getByText('主动运行 0 分 01 秒 · 用户等待 0 分 01 秒')).toBeInTheDocument()
})

it('aborts old requests on task switch and does not overlap a slow poll', async () => {
  const signals: AbortSignal[] = []
  vi.mocked(fetch).mockImplementation((_url, init) => {
    signals.push(init?.signal as AbortSignal)
    return new Promise((_resolve, reject) => init?.signal?.addEventListener('abort', () => reject(new Error('aborted'))))
  })
  const view = render(<TaskProgress taskId="t1" stream={stream()} />)
  await act(async () => { await vi.advanceTimersByTimeAsync(9000) })
  expect(fetch).toHaveBeenCalledTimes(1)
  view.rerender(<TaskProgress taskId="t2" stream={{ ...initialStreamState, connection: 'connecting' }} />)
  expect(signals[0].aborted).toBe(true)
  expect(fetch).toHaveBeenCalledTimes(2)
  view.unmount()
  expect(signals[1].aborted).toBe(true)
})
