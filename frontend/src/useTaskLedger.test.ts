// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { listTasks, type TaskListItem } from './api/client'
import { useTaskLedger } from './useTaskLedger'

vi.mock('./api/client', () => ({ listTasks: vi.fn() }))
afterEach(() => { cleanup(); vi.resetAllMocks() })

const task = (id: string) => ({ task_id: id, event_query: id }) as TaskListItem

it('loads older pages once, deduplicates, and preserves loaded history on refresh', async () => {
  const first = Array.from({ length: 20 }, (_, i) => task(`new-${i}`))
  vi.mocked(listTasks).mockResolvedValueOnce({ items: first, next_cursor: 'older' })
  const { result, rerender } = renderHook(({ revision }) => useTaskLedger(revision), { initialProps: { revision: 'a' } })
  await waitFor(() => expect(result.current.tasks).toHaveLength(20))
  let finish!: (value: { items: TaskListItem[]; next_cursor: null }) => void
  vi.mocked(listTasks).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  let pending!: Promise<void>
  act(() => { pending = result.current.loadMore(); void result.current.loadMore() })
  expect(listTasks).toHaveBeenCalledTimes(2)
  await act(async () => { finish({ items: [first[19], task('oldest')], next_cursor: null }); await pending })
  expect(result.current.tasks).toHaveLength(21)
  expect(result.current.tasks.at(-1)?.task_id).toBe('oldest')
  vi.mocked(listTasks).mockResolvedValueOnce({ items: first, next_cursor: 'older' }).mockResolvedValueOnce({ items: [task('oldest')], next_cursor: null })
  rerender({ revision: 'b' })
  await waitFor(() => expect(listTasks).toHaveBeenCalledTimes(4))
  await waitFor(() => expect(result.current.loading).toBe(false))
  expect(result.current.tasks).toHaveLength(21)
})

it('ignores an older load-more response after refresh changes the list', async () => {
  vi.mocked(listTasks).mockResolvedValueOnce({ items: Array.from({ length: 20 }, (_, i) => task(`current-${i}`)), next_cursor: 'older' })
  const { result, rerender } = renderHook(({ revision }) => useTaskLedger(revision), { initialProps: { revision: 'a' } })
  await waitFor(() => expect(result.current.loading).toBe(false))
  let finish!: (value: { items: TaskListItem[]; next_cursor: null }) => void
  vi.mocked(listTasks).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  let pending!: Promise<void>
  act(() => { pending = result.current.loadMore() })
  vi.mocked(listTasks).mockResolvedValueOnce({ items: [task('refreshed')], next_cursor: null })
  rerender({ revision: 'b' })
  await waitFor(() => expect(result.current.tasks[0]?.task_id).toBe('refreshed'))
  await act(async () => { finish({ items: [task('stale')], next_cursor: null }); await pending })
  expect(result.current.tasks.map(item => item.task_id)).toEqual(['refreshed'])
})

it('keeps loaded records after a page failure and allows retry', async () => {
  vi.mocked(listTasks).mockResolvedValueOnce({ items: Array.from({ length: 20 }, (_, i) => task(i === 0 ? 'saved' : `saved-${i}`)), next_cursor: 'older' })
  const { result } = renderHook(() => useTaskLedger('a'))
  await waitFor(() => expect(result.current.loading).toBe(false))
  vi.mocked(listTasks).mockRejectedValueOnce(new Error('暂时不可用'))
  await act(() => result.current.loadMore())
  expect(result.current.error).toBe('暂时不可用')
  expect(result.current.tasks[0]?.task_id).toBe('saved')
  vi.mocked(listTasks).mockResolvedValueOnce({ items: [task('older')], next_cursor: null })
  await act(() => result.current.loadMore())
  expect(result.current.tasks).toHaveLength(21)
  expect(result.current.error).toBe('')
})
