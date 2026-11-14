import { useCallback, useEffect, useRef, useState } from 'react'
import { listTasks, type TaskListItem } from './api/client'

export function useTaskLedger(revision: string) {
  const [tasks, setTasks] = useState<TaskListItem[]>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  const desiredCount = useRef(20)
  const generation = useRef(0)
  const busy = useRef(false)
  const controller = useRef<AbortController | null>(null)

  useEffect(() => {
    const current = ++generation.current
    controller.current?.abort()
    const request = new AbortController()
    controller.current = request
    busy.current = true
    setLoading(true)
    setError('')
    void (async () => {
      const items = new Map<string, TaskListItem>()
      let cursor: string | null = null
      do {
        const page = await listTasks(cursor, request.signal)
        if (generation.current !== current) return
        page.items.forEach(item => items.set(item.task_id, item))
        cursor = page.next_cursor
      } while (cursor && items.size < desiredCount.current)
      if (generation.current !== current) return
      setTasks([...items.values()])
      setNextCursor(cursor)
    })().catch((reason: unknown) => {
      if (generation.current === current && !request.signal.aborted) setError(reason instanceof Error ? reason.message : '无法读取任务记录。')
    }).finally(() => {
      if (generation.current === current && !request.signal.aborted) { busy.current = false; setLoading(false) }
    })
    return () => { request.abort(); generation.current++ }
  }, [revision, retry])

  const loadMore = useCallback(async () => {
    if (busy.current || !nextCursor) return
    busy.current = true
    const current = generation.current
    const request = new AbortController()
    controller.current = request
    setLoading(true)
    setError('')
    try {
      const page = await listTasks(nextCursor, request.signal)
      if (generation.current !== current) return
      setTasks(previous => [...new Map([...previous, ...page.items].map(item => [item.task_id, item])).values()])
      desiredCount.current += 20
      setNextCursor(page.next_cursor)
    } catch (reason) {
      if (generation.current === current && !request.signal.aborted) setError(reason instanceof Error ? reason.message : '无法读取更多任务记录。')
    } finally {
      if (generation.current === current && !request.signal.aborted) { busy.current = false; setLoading(false) }
    }
  }, [nextCursor])

  return { tasks, nextCursor, loading, error, loadMore, refresh: () => setRetry(value => value + 1) }
}
