import { useEffect, useRef, useState } from 'react'
import { getTaskProgress, type TaskProgressSnapshot } from '../api/client'
import { advanceTiming, type StreamState } from './state'

const connections: Record<string, string> = {
  connecting: '正在连接实时事件', connected: '实时事件已连接',
  reconnecting: '实时连接中断，正在重连', finished: '事件流已结束', idle: '尚未连接',
}
const stages: Record<string, string> = {
  plan: '规划检索', summarize: '生成陈述', reflect: '检查调查缺口', scope_review: '审查调查范围',
  moderation: '主持评审', verifier: '核验证据', reporter: '撰写报告',
  verification: '核验证据', report_analysis: '审查报告内容',
}

function duration(seconds: number): string {
  const value = Math.max(0, Math.floor(seconds))
  return `${Math.floor(value / 60)} 分 ${String(value % 60).padStart(2, '0')} 秒`
}

export function TaskProgress({ taskId, stream, revision = 0 }: {
  taskId: string; stream: StreamState & { connection: string }; revision?: number
}) {
  const [now, setNow] = useState(Date.now)
  const [snapshot, setSnapshot] = useState<{ value: TaskProgressSnapshot; received: number } | null>(null)
  const [unavailable, setUnavailable] = useState(false)
  const latestSeq = useRef(stream.lastSeq)
  latestSeq.current = stream.lastSeq

  useEffect(() => {
    let active = true
    let busy = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let controller: AbortController | undefined
    const refresh = async () => {
      if (!active || busy) return
      clearTimeout(timer)
      if (document.visibilityState === 'hidden') {
        timer = setTimeout(() => void refresh(), 15000)
        return
      }
      busy = true
      controller = new AbortController()
      const timeout = setTimeout(() => controller?.abort(), 10000)
      let terminal = false
      try {
        const value = await getTaskProgress(taskId, controller.signal)
        if (!active || value.task_id !== taskId) return
        setSnapshot({ value, received: Date.now() })
        setUnavailable(false)
        terminal = value.seq >= latestSeq.current && ['done', 'failed'].includes(value.status)
      } catch {
        if (active) setUnavailable(true)
      } finally {
        clearTimeout(timeout)
        busy = false
        if (active && !terminal && !['done', 'failed'].includes(stream.status)) {
          timer = setTimeout(() => void refresh(), 15000)
        }
      }
    }
    const onVisible = () => { if (document.visibilityState === 'visible') void refresh() }
    void refresh()
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      active = false
      controller?.abort()
      clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [taskId, revision, stream.status, stream.connection])

  const fresh = snapshot?.value.task_id === taskId && snapshot.value.seq >= stream.lastSeq ? snapshot : null
  const timing = fresh?.value.timing ?? stream.timing
  const status = fresh?.value.status ?? stream.status
  useEffect(() => {
    setNow(Date.now())
    if (['done', 'failed'].includes(status)) return
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [taskId, status])

  // A server snapshot is advanced from its receipt time, so client clock skew
  // does not become additional task runtime. SSE transitions correct status immediately.
  const clockSample = snapshot?.value.task_id === taskId ? snapshot : null
  const at = clockSample?.value.timing.sampled_at
    ? Date.parse(clockSample.value.timing.sampled_at) + Math.max(0, now - clockSample.received) : now
  const current = timing?.available ? advanceTiming(timing, new Date(at).toISOString()) : null
  const values = Object.values(stream.verification.claims)
  const verification = fresh?.value.verification ?? {
    total: stream.verification.total,
    complete: values.filter(v => v === 'complete').length,
    incomplete: values.filter(v => v === 'incomplete').length,
    skipped: values.filter(v => v === 'skipped').length,
  }
  const processed = (verification.complete ?? 0) + (verification.incomplete ?? 0) + (verification.skipped ?? 0)
  const models = snapshot?.value.task_id === taskId ? snapshot.value.model_calls : null
  const lastEvent = fresh?.value.last_event_at ?? stream.lastEventAt
  const age = lastEvent ? Math.max(0, (at - Date.parse(lastEvent)) / 1000) : null
  const budget = snapshot?.value.task_id === taskId ? snapshot.value.budget : null
  const tokens = Math.max(budget?.tokens_used ?? 0, stream.budget.tokensUsed)
  const limit = budget?.tokens_limit ?? stream.budget.tokensLimit
  const calls = Math.max(budget?.calls ?? 0, stream.budget.calls)
  const reserved = budget?.tokens_reserved ?? stream.budget.tokensReserved ?? 0

  return <><div className="budget-line"><span style={{ width: `${limit ? Math.min(100, tokens / limit * 100) : 0}%` }} /><small>{tokens.toLocaleString()} / {limit ? limit.toLocaleString() : '—'} tokens · {calls} 次调用</small></div>
    <div className="task-progress" aria-label="实时运行进度">
    {current && <p className="muted">主动运行 {duration(current.active_seconds ?? 0)} · 用户等待 {duration(current.waiting_seconds ?? 0)}</p>}
    {reserved > 0 && !['done', 'failed'].includes(status) && <p className="muted">最近采样：排队及在途请求预留 {reserved.toLocaleString()} token；预算是上限，核心内容达标即停止。</p>}
    <p className="muted">{connections[stream.connection] ?? stream.connection}
      {age !== null && Number.isFinite(age) && <> · 最近业务进展距今 {duration(age)}</>}
      {unavailable && ' · 进度校准暂不可用，计时按最后状态延续'}
    </p>
    {verification.total > 0 && <p>核验已处理 {processed}/{verification.total} 条 · 完整 {verification.complete ?? 0} · 未完成 {verification.incomplete ?? 0} · 跳过 {verification.skipped ?? 0}</p>}
    {models && <>
      <p className="muted">最近采样：模型处理中 {models.inflight_requests} 个 · 等待模型名额 {models.queued_requests} 个 · 重试请求 {models.retry_requests} 次</p>
      {!['done', 'failed'].includes(status) && models.activities?.length > 0 && <ul>{models.activities.map((item, index) => <li key={`${item.role}-${item.stage}-${index}`}>
        {stages[item.stage] ?? '模型处理'}：{item.status === 'queued' ? '等待共享模型名额' : '正在处理'}{item.attempt > 1 ? `（第 ${item.attempt} 次尝试）` : ''}
      </li>)}</ul>}
      <details><summary>模型耗时明细</summary><p className="muted">累计请求 {(models.request_ms / 60000).toFixed(1)} 分钟 · 累计排队 {(models.queue_ms / 60000).toFixed(1)} 分钟 · 失败请求 {models.failed_requests} 次。并发时间有重叠，不能相加作为任务总耗时；正在进行的请求在结束后计入累计时长。</p>
        {models.by_stage && <ul>{Object.entries(models.by_stage).sort((a, b) => b[1].request_ms - a[1].request_ms).map(([stage, item]) => <li key={stage}>
          {stages[stage] ?? ({ report_draft: '报告初稿', report_review: '报告审查', utility: '辅助处理' } as Record<string, string>)[stage] ?? '其他模型处理'}：{item.requests} 次请求 · 处理 {(item.request_ms / 60000).toFixed(1)} 分钟 · 排队 {(item.queue_ms / 60000).toFixed(1)} 分钟 · 失败 {item.failed} 次
        </li>)}</ul>}
      </details>
    </>}
  </div></>
}
