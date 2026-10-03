import { useEffect, useMemo, useState } from 'react'
import { getCommentQuestions, deepenCommentQuestion, type CommentQuestionData } from './api/client'

const statusLabels: Record<string, string> = { queued: '等待分析', running: '正在分析', complete: '对照完成', partial: '部分完成，已审内容保留', incomplete: '分析未完成，可重试', interrupted: '分析已中断，可重试', budget_limited: '原任务剩余额度不足', scope_review_incomplete: '关联样本审查未完成', no_new_evidence: '未取得新的合格材料', round_limit: '已达补查轮数上限', not_applicable: '当前问题不满足补查条件', claim_budget_limited: '陈述额度不足', verification_budget_limited: '核验额度不足', budget_exhausted: '剩余额度不足' }

export function CommentQuickRead({ taskId }: { taskId: string }) {
  const [data, setData] = useState<CommentQuestionData | null>(null)
  const [query, setQuery] = useState('')
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    setData(null); setError(''); setQuery(''); setPending(false)
    async function refresh() {
      try {
        const result = await getCommentQuestions(taskId, controller.signal)
        if (controller.signal.aborted) return
        setData(result)
        if (Object.values(result.jobs).some(j => ['queued', 'running'].includes(j.status))) timer = setTimeout(() => void refresh(), 2000)
      } catch (cause) {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : '评论读取失败')
      }
    }
    void refresh()
    return () => { controller.abort(); if (timer) clearTimeout(timer) }
  }, [taskId, revision])
  const block = data?.block
  const samples = useMemo(() => new Map((block?.samples ?? []).map(s => [s.id, s])), [block])
  const visibleSamples = useMemo(() => (block?.samples ?? []).filter(s => !query.trim() || s.text.includes(query.trim())), [block, query])
  const ungrouped = useMemo(() => { const grouped = new Set((block?.items ?? []).flatMap(q => q.observation_refs)); return (block?.observations ?? []).filter(o => !grouped.has(o.id)) }, [block])
  const busy = pending || Object.values(data?.jobs ?? {}).some(j => ['queued', 'running'].includes(j.status))
  async function analyze(id: string, mode: 'existing' | 'follow_up') {
    setPending(true); setError('')
    try { await deepenCommentQuestion(taskId, id, mode); setRevision(v => v + 1) }
    catch (cause) { setError(cause instanceof Error ? cause.message : '分析未启动') }
    finally { setPending(false) }
  }
  if (!block?.samples.length) return error ? <p role="alert">{error}</p> : null
  return <section className="comment-candidates comment-quick-read" aria-label="评论速读">
    <span className="section-kicker">评论 / 样本速读</span><h2>{block.analysis_mode === 'quick_read' ? '具体关切、理由与诉求' : '已审评论问题'}</h2>
    <p>仅代表已采集且通过审查的样本。各问题的样本可能重叠，样本数不能相加，也不等于参与人数；评论中的说法不证明事件事实。</p>
    {block.quick_read_status && <p role="status">{block.quick_read_status === 'complete' ? '本次合格样本的速读已完成。' : '本次速读部分未完成，已审索引和原话保留。'}</p>}
    {(Number(block.coverage.unclassified) > 0 || Number(block.coverage.index_review_incomplete) > 0) && <p className="muted">尚有 {block.coverage.unclassified ?? 0} 条合格样本未完成归纳，{block.coverage.index_review_incomplete ?? 0} 条索引审查未完成；原话仍可在下方搜索。</p>}
    {Number(block.coverage.relevant_without_index) > 0 && <p className="muted">{block.coverage.relevant_without_index} 条相关样本尚无合格索引，已审原话保留。</p>}
    {Number(block.coverage.scope_review_incomplete) > 0 && <p className="muted">另有 {block.coverage.scope_review_incomplete} 条原始评论尚未完成审查，未进入本次速读。</p>}
    {error && <p role="alert">{error}</p>}
    {block.items.map(question => {
      const observations = block.observations.filter(o => question.observation_refs.includes(o.id))
      const representatives = new Map<string, string>()
      observations.forEach(o => { if (!representatives.has(o.stance)) representatives.set(o.stance, o.comment_refs[0]) })
      const selected = [...new Set(representatives.values())]
      const job = data?.jobs[question.id]
      return <article className="host-review" key={question.id}>
        <h3>{question.title}</h3><small>{question.sample_count} 条去重样本</small>
        {question.summary && <p>{question.summary}</p>}
        {selected.map(ref => { const s = samples.get(ref); return s ? <blockquote key={ref}>{s.text}<br /><small>{s.platform} · <a href={s.source_url} target="_blank" rel="noreferrer">原帖</a></small></blockquote> : null })}
        <details><summary>不同理由与诉求，及全部关联原话</summary><ul>{observations.map(o => <li key={o.id}>{o.stance} · {o.text}<details><summary>回查原话</summary>{o.comment_refs.map(ref => <p key={ref}>{samples.get(ref)?.text}</p>)}</details></li>)}</ul></details>
        {question.comparisons.map(c => <div key={c.id}><p><strong>已有材料对照：</strong>{c.text}</p>{c.evidence_refs?.map(ref => { const source = data?.sources?.[ref]; return source ? <a key={ref} href={source.url} target="_blank" rel="noreferrer">{source.title} </a> : null })}</div>)}
        {question.judgements.map(j => <p key={j.id}><strong>{j.priority}：</strong>{j.response_action}<br />{j.uncertainty}</p>)}
        {job ? <p role="status">{statusLabels[job.status] ?? job.status}{job.message ? `：${job.message}` : ''}</p> : <p className="muted">{question.comparisons.length ? '已保留此前通过审查的证据对照。' : '深入分析未启动。'}</p>}
        {data?.deepening_available && <div className="candidate-actions"><button disabled={busy} onClick={() => void analyze(question.id, 'existing')}>对照已有材料</button>
          {question.publicly_verifiable && question.comparisons.some(c => ['partial', 'unanswered'].includes(c.status)) && <button className="quiet" disabled={busy} onClick={() => void analyze(question.id, 'follow_up')}>开启公开补查</button>}</div>}
      </article>
    })}
    {ungrouped.length > 0 && <details><summary>待归类的已审短索引（{ungrouped.length} 条）</summary><ul>{ungrouped.map(o => <li key={o.id}>{o.text}<details><summary>回查原话</summary>{o.comment_refs.map(ref => <p key={ref}>{samples.get(ref)?.text}</p>)}</details></li>)}</ul></details>}
    <details><summary>搜索全部合格原话（{block.samples.length} 条）</summary><label>评论关键词<input value={query} onChange={e => setQuery(e.target.value)} placeholder="输入关切或原话中的词" /></label><p>{visibleSamples.length} 条匹配样本</p><ol>{visibleSamples.map(s => <li key={s.id}><p>{s.text}</p><small>{s.platform} · {s.published_at ?? '时间未知'} · <a href={s.source_url} target="_blank" rel="noreferrer">原帖</a></small></li>)}</ol></details>
  </section>
}
