import { useEffect, useReducer } from 'react'
import { applyEvent, initialStreamState, type TaskEvent } from './state'

const eventTypes = [
  'task.status', 'agent.status', 'agent.token', 'search.result', 'evidence.added',
  'claim.added', 'forum.message', 'host.review', 'loop.round', 'verify.progress',
  'report.section', 'report.done', 'budget.update', 'warning', 'error',
]

export function useTaskStream(taskId: string | null, revision = 0) {
  const [state, dispatch] = useReducer(applyEvent, initialStreamState)

  useEffect(() => {
    dispatch({ event: '__reset__', task_id: taskId ?? '', seq: 0, ts: '', data: {} })
    if (!taskId) return undefined
    const source = new EventSource(`/api/tasks/${taskId}/events?since_seq=0`)
    const onMessage = (raw: MessageEvent<string>) => {
      if (typeof raw.data !== 'string' || !raw.data) return
      let message: TaskEvent
      try {
        message = JSON.parse(raw.data) as TaskEvent
      } catch {
        return
      }
      dispatch(message)
      if (message.event === 'task.status' && ['done', 'failed', 'paused'].includes(String(message.data.status))) {
        source.close()
      }
    }
    eventTypes.forEach((name) => source.addEventListener(name, onMessage as EventListener))
    // 运行中断线由 EventSource 自动重连；终态在消息处理器主动关闭。
    return () => source.close()
  }, [taskId, revision])

  return state
}
