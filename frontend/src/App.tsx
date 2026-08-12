import { FormEvent, useEffect, useMemo, useState } from 'react'
import { createTask, listTasks, resumeTask, type TaskListItem } from './api/client'
import { useTaskStream } from './events/useTaskStream'
import './styles.css'

const phaseLabels: Record<string, string> = {
  planning: '制定调查计划',
  searching: '检索公开来源',
  summarizing: '生成证据化陈述',
  verifying: '逐条交叉核验',
  reporting: '编排速览报告',
  finished: '分析完成',
}

function App() {
  const initialTask = useMemo(() => new URLSearchParams(location.search).get('task'), [])
  const [taskId, setTaskId] = useState<string | null>(initialTask)
  const [query, setQuery] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [formError, setFormError] = useState('')
  const [tasks, setTasks] = useState<TaskListItem[]>([])
  const stream = useTaskStream(taskId)

  useEffect(() => {
    void listTasks().then(setTasks).catch(() => setTasks([]))
  }, [taskId, stream.status])

  async function resume(item: TaskListItem) {
    setFormError('')
    try {
      await resumeTask(item.task_id)
      history.replaceState(null, '', `?task=${encodeURIComponent(item.task_id)}`)
      setTaskId(item.task_id)
    } catch (error) {
      setFormError(error instanceof Error ? error.message : '续跑失败')
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!query.trim()) return
    setSubmitting(true)
    setFormError('')
    try {
      const task = await createTask(query.trim())
      history.replaceState(null, '', `?task=${encodeURIComponent(task.task_id)}`)
      setTaskId(task.task_id)
    } catch (error) {
      setFormError(error instanceof Error ? error.message : '创建任务失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <>
      <header className="masthead">
        <div className="brand-line">
          <span className="case-token">YX / 00</span>
          <span>公开材料核验台</span>
          <span className="edition">M0 · SINGLE AGENT</span>
        </div>
        <div className="hero-copy">
          <div>
            <p className="hero-kicker">TRACE · VERIFY · REPORT</p>
            <h1>让每条结论<br /><em>回到原始证据</em></h1>
          </div>
          <p className="hero-note">输入公共事件。系统会留下检索、存证、核验与报告生成的连续记录。</p>
        </div>
      </header>
      <main>
        <section className="query-panel">
          <span className="section-kicker">01 / 建立调查</span>
          <h2>这次要核查什么？</h2>
          <form onSubmit={submit}>
            <input value={query} onChange={(event) => setQuery(event.target.value)} maxLength={200} placeholder="例如：某企业产品召回事件" aria-label="事件名称" />
            <button disabled={submitting || !query.trim()}>{submitting ? '正在建立证据链…' : '开始调查'}</button>
          </form>
          {formError && <p className="error">{formError}</p>}
        </section>

        {tasks.length > 0 && (
          <section className="task-shelf">
            <span className="section-kicker">历史 / 可续跑任务</span>
            <div className="task-list">
              {tasks.map((item) => (
                <article key={item.task_id}>
                  <div><strong>{item.event_query}</strong><small>{item.status} · {item.task_id}</small></div>
                  {item.resumable ? (
                    <button type="button" onClick={() => void resume(item)}>从检查点续跑</button>
                  ) : item.report_id ? (
                    <button type="button" onClick={() => setTaskId(item.task_id)}>查看运行记录</button>
                  ) : <span className="muted">不可续跑</span>}
                </article>
              ))}
            </div>
          </section>
        )}

        {taskId && (
          <section className="desk-grid" aria-live="polite">
            <article className="status-card">
              <span className="section-kicker">02 / 当前状态</span>
              <div className={`status-dot ${stream.status}`} />
              <h2>{phaseLabels[stream.phase] ?? stream.agentPhase}</h2>
              <p className="mono">{taskId}</p>
              <div className="sequence-readout"><small>EVENT SEQ</small><strong>{String(stream.lastSeq).padStart(3, '0')}</strong></div>
              <p>刷新页面后，从这个序号继续补发，不重复展示。</p>
              {stream.reportUrl && <a className="report-link" href={stream.reportUrl}>打开可核验速览报告 →</a>}
            </article>
            <article className="evidence-column">
              <span className="section-kicker">03 / 证据台账</span>
              <h2>证据流</h2>
              {stream.evidence.length === 0 && <p className="muted">等待搜索结果进入证据库…</p>}
              {stream.evidence.map((item) => (
                <div className="evidence-row" key={item.id}>
                  <span className="mono">{item.id}</span>
                  <div><strong>{item.title}</strong><small>{item.source} · L{item.tier}</small></div>
                </div>
              ))}
            </article>
            <article className="decision-column">
              <span className="section-kicker">04 / 增益判定</span>
              <h2>轮次判定</h2>
              {stream.decisions.length === 0 && <p className="muted">Agent 尚未进入反思节点。</p>}
              {stream.decisions.map((item, index) => <p className="decision" key={`${index}-${item}`}>{item}</p>)}
              {stream.errors.map((item, index) => <p className="error" key={`${index}-${item}`}>{item}</p>)}
            </article>
          </section>
        )}
      </main>
      <footer>仅处理公开材料 · 原文未取得时如实标注 · M0 单 Agent 竖切</footer>
    </>
  )
}

export default App
