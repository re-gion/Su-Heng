import { useEffect, useReducer } from 'react'
import { applyEvent, initialStreamState, type TaskEvent } from './state'

const eventTypes = [
  'task.status', 'agent.status', 'agent.token', 'search.result', 'evidence.added',
  'claim.added', 'forum.message', 'host.review', 'loop.round', 'verify.progress',
  'report.section', 'report.done', 'budget.update', 'warning', 'error',
]

export function useTaskStream(taskId: string | null) {
  const [state, dispatch] = useReducer(applyEvent, initialStreamState)

  useEffect(() => {
    dispatch({ event: '__reset__', task_id: taskId ?? '', seq: 0, ts: '', data: {} })
    if (!taskId) return undefined
    const source = new EventSource(`/api/tasks/${taskId}/events?since_seq=0`)
    const onMessage = (raw: MessageEvent<string>) => {
      const message = JSON.parse(raw.data) as TaskEvent
      dispatch(message)
    }
    eventTypes.forEach((name) => source.addEventListener(name, onMessage as EventListener))
    source.onerror = () => {
      // EventSource 会自动重连；服务端通过 Last-Event-ID/since_seq 保证补发。
    }
    return () => source.close()
  }, [taskId])

  return state
}
