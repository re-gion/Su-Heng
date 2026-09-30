// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getTaskProgress } from '../api/client'
import { useTaskStream } from './useTaskStream'

vi.mock('../api/client', () => ({ getTaskProgress: vi.fn() }))

class FakeEventSource {
  static latest: FakeEventSource
  listeners = new Map<string, EventListener[]>()
  close = vi.fn()

  constructor(public url: string) { FakeEventSource.latest = this }

  addEventListener(name: string, listener: EventListener) {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), listener])
  }

  emit(status: string, seq: number) {
    const data = JSON.stringify({ event: 'task.status', task_id: 't1', seq,
      ts: '2026-09-29T00:00:00Z', data: { status, phase: 'forum' } })
    for (const listener of this.listeners.get('task.status') ?? []) {
      listener(new MessageEvent('task.status', { data }))
    }
  }
}

describe('useTaskStream historical status replay', () => {
  beforeEach(() => { vi.stubGlobal('EventSource', FakeEventSource); vi.mocked(getTaskProgress).mockReset() })
  afterEach(() => { cleanup(); vi.unstubAllGlobals() })

  it('keeps receiving events after a historical failed state was resumed', async () => {
    let resolve!: (value: { status: string; seq: number }) => void
    vi.mocked(getTaskProgress).mockImplementation(() => new Promise((accept) => { resolve = accept as typeof resolve }) as ReturnType<typeof getTaskProgress>)
    const { result } = renderHook(() => useTaskStream('t1'))
    const source = FakeEventSource.latest
    act(() => { source.emit('failed', 2); source.emit('running', 3) })
    resolve({ status: 'failed', seq: 2 })
    await waitFor(() => expect(result.current.status).toBe('running'))
    expect(source.close).not.toHaveBeenCalled()
  })

  it('closes when the latest persisted status is failed', async () => {
    vi.mocked(getTaskProgress).mockResolvedValue({ status: 'failed', seq: 2 } as Awaited<ReturnType<typeof getTaskProgress>>)
    const { result } = renderHook(() => useTaskStream('t1'))
    const source = FakeEventSource.latest
    act(() => source.emit('failed', 2))
    await waitFor(() => expect(result.current.connection).toBe('finished'))
    expect(source.close).toHaveBeenCalledTimes(1)
  })
})
