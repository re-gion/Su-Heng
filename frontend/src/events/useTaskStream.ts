import { useEffect, useReducer, useState } from 'react'
import { getTaskProgress } from '../api/client'
import { applyEvent, initialStreamState, type TaskEvent } from './state'

const eventTypes = [
  'task.status', 'agent.status', 'agent.token', 'search.result', 'evidence.added',
  'claim.added', 'forum.message', 'host.review', 'loop.round', 'verify.progress',
  'report.section', 'report.done', 'budget.update', 'warning', 'error',
]

export function useTaskStream(taskId: string | null, revision = 0) {
  const [state, dispatch] = useReducer(applyEvent, initialStreamState)
  const [connection, setConnection] = useState('connecting')

  useEffect(() => {
    dispatch({ event: '__reset__', task_id: taskId ?? '', seq: 0, ts: '', data: {} })
    setConnection(taskId ? 'connecting' : 'idle')
    if (!taskId) return undefined
    let active = true
    let lastReceivedSeq = 0
    const statusChecks = new Set<AbortController>()
    const source = new EventSource(`/api/tasks/${taskId}/events`)
    source.addEventListener('open', () => { if (active) setConnection('connected') })
    source.addEventListener('error', () => { if (active) setConnection('reconnecting') })
    const onMessage = (raw: MessageEvent<string>) => {
      if (!active) return
      if (typeof raw.data !== 'string' || !raw.data) return
      let message: TaskEvent
      try {
        message = JSON.parse(raw.data) as TaskEvent
      } catch {
        return
      }
      lastReceivedSeq = Math.max(lastReceivedSeq, message.seq)
      dispatch(message)
      // failed 可续跑。历史补放中的旧 failed 不能截断后续 running 事件。
      if (message.event === 'task.status' && message.data.status === 'failed') {
        const controller = new AbortController()
        statusChecks.add(controller)
        void getTaskProgress(taskId, controller.signal).then((current) => {
          if (active && lastReceivedSeq === message.seq && current.status === 'failed' && current.seq >= message.seq) {
            setConnection('finished')
            source.close()
          }
        }).catch(() => {
          // 校准失败时继续接收事件；EventSource 会自行重连。
        }).finally(() => statusChecks.delete(controller))
      }
      if (message.event === 'task.status' && message.data.status === 'done') {
        setConnection('finished')
        source.close()
      }
    }
    eventTypes.forEach((name) => source.addEventListener(name, onMessage as EventListener))
    // 运行中断线由 EventSource 自动重连；终态在消息处理器主动关闭。
    return () => { active = false; statusChecks.forEach(controller => controller.abort()); source.close() }
  }, [taskId, revision])

  return { ...state, connection }
}
