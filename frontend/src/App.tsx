import { FormEvent, useEffect, useMemo, useState } from 'react'
import {
  createTask,
  deleteTask,
  getConfig,
  getDataStatus,
  getCommentPluginStatus,
  getCommentCandidates,
  getTaskDetail,
  openCommentLogin,
  clearCommentProfile,
  submitCommentSelection,
  stopCommentCollection,
  listTasks,
  pauseTask,
  resumeTask,
  stopTask,
  testConfig,
  updateConfig,
  type PublicConfig,
  type DataStatus,
  type TaskListItem,
  type CommentPluginStatus,
  type CommentCandidates,
  type TaskDetail,
} from './api/client'
import { useTaskStream } from './events/useTaskStream'
import './styles.css'
import './form.css'
import './data-depth.css'

const agents = [
  ['fact_investigator', '事实调查', '核对事件本身与官方材料'],
  ['media_propagation', '媒体传播', '梳理采编主体与报道口径'],
  ['history_insight', '历史洞察', '本地历史库优先，搜索回溯补充'],
  ['comment_insight', '评论洞察', '仅分析用户确认帖子的脱敏评论样本'],
] as const

const phaseLabels: Record<string, string> = {
  planning: '制定调查计划', forum: '三路协作调查', searching: '检索公开来源',
  summarizing: '生成证据化陈述', verifying: '逐条交叉核验', reporting: '编排完整专报',
  finished: '分析完成', pausing: '正在停到安全点', stopping: '封存证据并出报告', skipped: '快速模式未启用',
  comment_selection: '等待确认高价值帖子', comment_collection: '采集确认帖子的评论', comment_analysis: '分析脱敏评论样本', awaiting_selection: '候选帖子待确认',
}

function Settings({ onClose }: { onClose: () => void }) {
  const [config, setConfig] = useState<PublicConfig | null>(null)
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [notice, setNotice] = useState('')
  const [providerOrder, setProviderOrder] = useState<string[]>([])
  const [commentStatus, setCommentStatus] = useState<CommentPluginStatus | null>(null)
  useEffect(() => { void getConfig().then((value) => { setConfig(value); setProviderOrder(value.search.provider_order) }).catch((error) => setNotice(String(error))) }, [])
  useEffect(() => { void getCommentPluginStatus().then(setCommentStatus).catch(() => setCommentStatus(null)) }, [])
  if (!config) return <main className="settings-page"><p>{notice || '正在读取配置…'}</p></main>
  const currentConfig = config
  const roles = Object.entries(currentConfig.llm.roles)
  const set = (key: string, value: string) => setDraft((valueBefore) => ({ ...valueBefore, [key]: value }))
  async function save(event: FormEvent) {
    event.preventDefault(); setNotice('正在保存…')
    const rolePayload: Record<string, Record<string, string>> = {}
    for (const [role] of roles) {
      const values = Object.fromEntries(['api_key', 'base_url', 'model'].flatMap((field) => draft[`${role}.${field}`] ? [[field, draft[`${role}.${field}`]]] : []))
      if (Object.keys(values).length) rolePayload[role] = values
    }
    const searchKeys = Object.fromEntries(Object.keys(currentConfig.search.keys).flatMap((name) => draft[`search.${name}`] ? [[name, draft[`search.${name}`]]] : []))
    try {
      const updated = await updateConfig({ llm: { roles: rolePayload }, search: { keys: searchKeys, provider_order: providerOrder }, comments: { enabled: draft['comments.enabled'] ? draft['comments.enabled'] === 'true' : currentConfig.comments.enabled } })
      setConfig(updated); setProviderOrder(updated.search.provider_order); setDraft({}); setNotice('已保存。新配置会用于下一次任务。')
    } catch (error) { setNotice(error instanceof Error ? error.message : '保存失败') }
  }
  async function test(kind: 'llm' | 'search', name: string) {
    setNotice(`正在测试 ${name}…`)
    try {
      const result = await testConfig(kind === 'llm' ? { kind, role: name } : { kind, provider: name })
      setNotice(result.ok ? `${name} 连接成功 · ${result.latency_ms}ms · ${result.sample ?? 'OK'}` : `${name} 连接失败：${result.error?.message ?? '未知错误'}`)
    } catch (error) {
      setNotice(`${name} 连接失败：${error instanceof Error ? error.message : '网络异常'}`)
    }
  }
  return <main className="settings-page">
    <div className="settings-head"><div><span className="section-kicker">V2 / 配置中枢</span><h1>角色、检索链与评论插件</h1><p>设置页覆盖 `.env`，密钥只显示脱敏值，不写回文件。</p></div><button type="button" onClick={onClose}>返回调查台</button></div>
    <form className="settings-form" onSubmit={save}>
      <section><h2>LLM 角色三元组</h2><p className="muted">留空继续继承当前有效值；核验器与生成模型异构时，交叉核验更有意义。</p>
        <div className="role-config-grid">{roles.map(([role, value]) => <article key={role}>
          <header><strong>{role}</strong><button type="button" onClick={() => void test('llm', role)}>测试</button></header>
          <label>API Key<input type="password" value={draft[`${role}.api_key`] ?? ''} onChange={(event) => set(`${role}.api_key`, event.target.value)} placeholder={value.effective.api_key ?? '未配置'} /></label>
          <label>Base URL<input value={draft[`${role}.base_url`] ?? ''} onChange={(event) => set(`${role}.base_url`, event.target.value)} placeholder={value.effective.base_url} /></label>
          <label>Model<input value={draft[`${role}.model`] ?? ''} onChange={(event) => set(`${role}.model`, event.target.value)} placeholder={value.effective.model} /></label>
          <small>{Object.values(value.source).join(' / ')}</small>
        </article>)}</div>
      </section>
      <section><h2>搜索降级链</h2><div className="provider-order" aria-label="搜索优先级">{providerOrder.map((name, index) => <span key={name}><strong>{index + 1}. {name}</strong><button type="button" disabled={index === 0} onClick={() => setProviderOrder((order) => { const next = [...order]; [next[index - 1], next[index]] = [next[index], next[index - 1]]; return next })}>↑</button><button type="button" disabled={index === providerOrder.length - 1} onClick={() => setProviderOrder((order) => { const next = [...order]; [next[index], next[index + 1]] = [next[index + 1], next[index]]; return next })}>↓</button></span>)}</div><div className="search-config-grid">{Object.entries(currentConfig.search.keys).map(([name, masked]) => <label key={name}><span>{name}<button type="button" onClick={() => void test('search', name)}>测试</button></span><input type="password" value={draft[`search.${name}`] ?? ''} onChange={(event) => set(`search.${name}`, event.target.value)} placeholder={masked ?? '未配置'} /></label>)}</div></section>
      <section><h2>登录态评论插件</h2><p className="muted">仅限本机使用。登录信息留在专用浏览器 profile，不写入数据库或报告。</p><label className="toggle-row"><input type="checkbox" checked={(draft['comments.enabled'] ? draft['comments.enabled'] === 'true' : currentConfig.comments.enabled)} disabled={!commentStatus?.available} onChange={(event) => set('comments.enabled', String(event.target.checked))} />启用智能选帖与登录态评论采集</label><p className="muted">{commentStatus?.risk_notice}</p><div className="platform-login-grid">{commentStatus?.platforms.map((item) => <article key={item.platform}><strong>{item.platform}</strong><small>{item.browser_open ? '登录浏览器已打开' : item.profile_present ? '已有本地 profile' : '尚未登录'}</small><button type="button" disabled={!commentStatus.available || !currentConfig.comments.enabled} onClick={() => void openCommentLogin(item.platform).then(() => setNotice(`${item.platform} 登录浏览器已打开`)).catch((error) => setNotice(String(error)))}>打开登录浏览器</button><button type="button" className="quiet" disabled={!item.profile_present} onClick={() => { if (confirm(`确定清除 ${item.platform} 的专用浏览器登录数据？`)) void clearCommentProfile(item.platform).then(() => getCommentPluginStatus().then(setCommentStatus)) }}>清除登录</button></article>)}</div></section>
      <div className="settings-save"><button>保存配置</button><span>{notice}</span></div>
    </form>
  </main>
}

function App() {
  const initialTask = useMemo(() => new URLSearchParams(location.search).get('task'), [])
  const [taskId, setTaskId] = useState<string | null>(initialTask)
  const [streamRevision, setStreamRevision] = useState(0)
  const [screen, setScreen] = useState<'desk' | 'settings'>('desk')
  const [query, setQuery] = useState('')
  const [note, setNote] = useState('')
  const [timeFrom, setTimeFrom] = useState('')
  const [timeTo, setTimeTo] = useState('')
  const [depth, setDepth] = useState('standard')
  const [sourceScope, setSourceScope] = useState<'auto' | 'domestic' | 'global'>('auto')
  const [sourceLanguages, setSourceLanguages] = useState('zh,en')
  const [commentMode, setCommentMode] = useState<'off' | 'smart' | 'manual' | 'hybrid'>('off')
  const [commentUrls, setCommentUrls] = useState('')
  const [commentCandidates, setCommentCandidates] = useState<CommentCandidates | null>(null)
  const [taskDetail, setTaskDetail] = useState<TaskDetail | null>(null)
  const [selectedCandidates, setSelectedCandidates] = useState<string[]>([])
  const [supplementalCommentUrls, setSupplementalCommentUrls] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [formError, setFormError] = useState('')
  const [tasks, setTasks] = useState<TaskListItem[]>([])
  const [dataStatus, setDataStatus] = useState<DataStatus | null>(null)
  const stream = useTaskStream(taskId, streamRevision)
  useEffect(() => { void listTasks().then(setTasks).catch(() => setTasks([])) }, [taskId, stream.status])
  useEffect(() => { void getDataStatus().then(setDataStatus).catch(() => setDataStatus(null)) }, [])
  useEffect(() => {
    if (!taskId) {
      setTaskDetail(null)
      return
    }
    void getTaskDetail(taskId).then(setTaskDetail).catch(() => setTaskDetail(null))
  }, [taskId, streamRevision])
  useEffect(() => { if (taskId && stream.phase === 'comment_selection') void getCommentCandidates(taskId).then((value) => { setCommentCandidates(value); setSelectedCandidates(value.items.slice(0, value.budgets.posts).map((item) => item.id)) }).catch(() => setCommentCandidates(null)); else setCommentCandidates(null) }, [taskId, stream.phase])

  function selectTask(id: string) { history.replaceState(null, '', `?task=${encodeURIComponent(id)}`); setTaskId(id); setStreamRevision((value) => value + 1) }
  function clearTask() { history.replaceState(null, '', location.pathname); setTaskId(null); setStreamRevision((value) => value + 1) }
  async function action(work: () => Promise<unknown>) { setFormError(''); try { await work() } catch (error) { setFormError(error instanceof Error ? error.message : '操作失败') } }
  async function submit(event: FormEvent) {
    event.preventDefault(); if (!query.trim()) return; setSubmitting(true); setFormError('')
    try { const task = await createTask(query.trim(), depth, note.trim(), timeFrom, timeTo, { sourceScope, sourceLanguages: sourceLanguages.split(',').map((item) => item.trim()).filter(Boolean).slice(0, 3), commentMode, commentUrls: commentUrls.split(/\r?\n/).map((item) => item.trim()).filter(Boolean) }); selectTask(task.task_id) }
    catch (error) { setFormError(error instanceof Error ? error.message : '创建任务失败') }
    finally { setSubmitting(false) }
  }
  if (screen === 'settings') return <Settings onClose={() => setScreen('desk')} />
  const budgetPercent = stream.budget.tokensLimit ? Math.min(100, stream.budget.tokensUsed / stream.budget.tokensLimit * 100) : 0
  const visibleAgents = agents.filter(([key]) => key !== 'comment_insight' || commentMode !== 'off' || stream.phase.startsWith('comment_') || Boolean(stream.agents.comment_insight))
  const hasCommentCandidates = Boolean(commentCandidates?.items.length)
  const allowsSupplementalCommentUrls = ['manual', 'hybrid'].includes(commentMode)
  const detailPhase = taskDetail?.phase ?? stream.phase
  const detailStatus = taskDetail?.status ?? stream.status
  const canCommentSelection = detailPhase === 'comment_selection' && detailStatus === 'paused'
  const canStopTask = ['running', 'pausing', 'paused'].includes(detailStatus)

  return <>
    <header className="masthead"><div className="brand-line"><span className="case-token">YX / V2</span><span>多语言证据与评论样本协作核验台</span><button className="text-button" disabled={dataStatus?.demo_mode} onClick={() => setScreen('settings')}>{dataStatus?.demo_mode ? '演示站只读' : '配置中枢 ↗'}</button></div>
      <div className="hero-copy"><div><p className="hero-kicker">GLOBAL SOURCES · ONE EVIDENCE LEDGER</p><h1>多语言调查，<br /><em>评论样本深挖</em></h1></div><p className="hero-note">事实、传播与历史三路并行，评论洞察仅分析用户确认帖子的脱敏样本；所有结论回到可点击原文。</p></div>
    </header>
    <main>
      <section className="query-panel"><span className="section-kicker">01 / 建立调查</span><h2>这次要核查什么？</h2><form onSubmit={submit}>
        <div className="query-fields"><input value={query} onChange={(event) => setQuery(event.target.value)} maxLength={200} placeholder="例如：某企业产品召回事件" aria-label="事件名称" /><input value={note} onChange={(event) => setNote(event.target.value)} placeholder="可选：特别关注点" aria-label="特别关注点" /><label className="date-field"><span>起始日期</span><input type="date" value={timeFrom} max={timeTo || undefined} onChange={(event) => setTimeFrom(event.target.value)} /></label><label className="date-field"><span>结束日期</span><input type="date" value={timeTo} min={timeFrom || undefined} onChange={(event) => setTimeTo(event.target.value)} /></label></div>
        <div className="v2-options"><label>调查深度<select value={depth} onChange={(event) => setDepth(event.target.value)} aria-label="调查深度"><option value="quick">快速</option><option value="standard">标准</option><option value="deep">深入</option></select></label><label>信源范围<select value={sourceScope} onChange={(event) => setSourceScope(event.target.value as typeof sourceScope)}><option value="auto">自动判断</option><option value="domestic">国内优先</option><option value="global">境外扩展</option></select></label><label>信源语言<input value={sourceLanguages} onChange={(event) => setSourceLanguages(event.target.value)} placeholder="zh,en" /></label><label>评论深挖<select value={commentMode} onChange={(event) => setCommentMode(event.target.value as typeof commentMode)}><option value="off">关闭</option><option value="smart">智能选帖</option><option value="manual">指定 URL</option><option value="hybrid">混合</option></select></label></div>{['manual', 'hybrid'].includes(commentMode) && <textarea value={commentUrls} onChange={(event) => setCommentUrls(event.target.value)} placeholder="每行一个微博/B站/知乎/小红书/抖音/快手/贴吧帖子 URL" aria-label="指定帖子 URL" />}
        <button disabled={submitting || !query.trim()}>{submitting ? '正在建立协作任务…' : '启动三路调查'}</button>
      </form>{formError && <p className="error">{formError}</p>}</section>

      {dataStatus && <section className="data-depth"><div><span className="section-kicker">DATA / 数据纵深</span><h2>本地辅助层</h2><p>只对已覆盖事件优先命中；没有命中时，历史 Agent 会如实回退搜索。</p></div><dl><div><dt>数据资产</dt><dd>{dataStatus.assets}</dd></div><div><dt>历史事件</dt><dd>{dataStatus.historical_events}</dd></div><div><dt>热榜采集点</dt><dd>{dataStatus.hot_snapshots}</dd></div><div><dt>覆盖截止</dt><dd>{dataStatus.hot_coverage.to?.slice(0, 16) ?? '尚未采集'}</dd></div></dl></section>}

      {tasks.length > 0 && <section className="task-shelf"><span className="section-kicker">历史 / 任务台账</span><div className="task-list">{tasks.map((item) => <article key={item.task_id} className={item.task_id === taskId ? 'active' : ''}><button className="task-title" onClick={() => selectTask(item.task_id)}><strong>{item.event_query}</strong><small>{item.status} · {item.task_id}</small></button><div className="task-actions">{item.comment_selection_required && <button onClick={() => selectTask(item.task_id)}>确认评论</button>}{item.resumable && <button onClick={() => void action(async () => { await resumeTask(item.task_id); selectTask(item.task_id) })}>续跑</button>}{!['running', 'pausing', 'stopping'].includes(item.status) && <button className="quiet" onClick={() => void action(async () => { await deleteTask(item.task_id); if (item.task_id === taskId) clearTask(); setTasks(await listTasks()) })}>删除</button>}</div></article>)}</div></section>}

      {taskId && <>
        <section className="run-console"><div className="run-status"><span className={`status-dot ${stream.status}`} /><div><span className="section-kicker">02 / {stream.status}</span><h2>{phaseLabels[stream.phase] ?? stream.phase}</h2><p className="mono">{taskId} · EVENT {String(stream.lastSeq).padStart(3, '0')}</p></div><div className="run-actions">{stream.status === 'running' && canStopTask && <button onClick={() => void action(() => pauseTask(taskId))}>暂停</button>}{stream.phase === 'comment_collection' && <button onClick={() => void action(() => stopCommentCollection(taskId))}>停止评论采集</button>}{canStopTask && <button onClick={() => void action(async () => { await stopTask(taskId); selectTask(taskId) })}>停止并出报告</button>}{stream.reportUrl && <a className="report-link" href={stream.reportUrl}>打开完整专报 →</a>}</div></div>
          <div className="budget-line"><span style={{ width: `${budgetPercent}%` }} /><small>{stream.budget.tokensUsed.toLocaleString()} / {stream.budget.tokensLimit.toLocaleString() || '—'} tokens · {stream.budget.calls} 次调用</small></div></section>

        <section className="agent-rail"><span className="section-kicker">03 / 并行调查席</span><div className="agent-panels">{visibleAgents.map(([key, label, description]) => <article key={key} data-agent={key}><div className="agent-index">{key === 'fact_investigator' ? 'A' : key === 'media_propagation' ? 'B' : key === 'history_insight' ? 'C' : 'D'}</div><h3>{label}</h3><p>{description}</p><strong>{phaseLabels[stream.agents[key]] ?? stream.agents[key] ?? '等待调度'}</strong></article>)}</div></section>

        {canCommentSelection && commentCandidates && <section className="comment-candidates"><span className="section-kicker">V2 / 评论候选确认</span><h2>选择值得进入的评论区</h2><p>最多 {commentCandidates.budgets.posts} 帖，每帖 {commentCandidates.budgets.comments_per_post} 条。确认前系统不会访问登录态内容。</p>{hasCommentCandidates ? <><div>{commentCandidates.items.map((item) => <label key={item.id} className="candidate-card"><input type="checkbox" checked={selectedCandidates.includes(item.id)} disabled={!selectedCandidates.includes(item.id) && selectedCandidates.length >= commentCandidates.budgets.posts} onChange={(event) => setSelectedCandidates((current) => event.target.checked ? [...current, item.id] : current.filter((id) => id !== item.id))} /><span><strong>{item.platform} · {item.title}</strong><small>评分 {item.score.toFixed(1)} · {item.reasons.join(' / ')} · {item.login_profile_present ? '已有登录 profile' : '需先登录'}</small><a href={item.url} target="_blank" rel="noreferrer">查看原帖</a></span></label>)}</div>{allowsSupplementalCommentUrls && <textarea value={supplementalCommentUrls} onChange={(event) => setSupplementalCommentUrls(event.target.value)} placeholder="可选：每行补充一个帖子 URL，确认时一并采集" aria-label="补充帖子 URL" />}<div className="candidate-actions"><button disabled={!selectedCandidates.length && !(allowsSupplementalCommentUrls && supplementalCommentUrls.trim())} onClick={() => void action(async () => { await submitCommentSelection(taskId, { action: 'approve', candidate_ids: selectedCandidates, urls: supplementalCommentUrls.split(/\r?\n/).map((item) => item.trim()).filter(Boolean) }); selectTask(taskId) })}>确认并采集</button><button className="quiet" onClick={() => void action(async () => { await submitCommentSelection(taskId, { action: 'skip' }); selectTask(taskId) })}>跳过评论，继续报告</button></div></> : <><p className="muted">当前没有可审批的系统候选。你可以直接跳过评论继续报告，或返回调整调查范围后重试。</p><div className="candidate-actions"><button className="quiet" onClick={() => void action(async () => { await submitCommentSelection(taskId, { action: 'skip' }); selectTask(taskId) })}>跳过评论，继续报告</button></div></>}</section>}

        <section className="collaboration-grid"><article className="forum-board"><span className="section-kicker">04 / 论坛黑板</span><h2>Agent 原始讨论流</h2>{stream.forum.length === 0 ? <p className="muted">等待各调查席发言…</p> : stream.forum.map((item, index) => <div className={`forum-message ${item.type}`} key={`${index}-${item.content}`}><span>R{item.round} · {item.agent}</span><strong>{item.type}</strong><p>{item.content}</p></div>)}</article>
          <article className="host-board"><span className="section-kicker">05 / 主持人评审</span><h2>缺口，不是表演出来的争论</h2>{stream.hostReviews.length === 0 ? <p className="muted">首轮调查汇合后开始评审。</p> : stream.hostReviews.map((review, index) => <div className="host-review" key={`${index}-${review.reason}`}><strong>{review.release ? '放行' : '继续补查'}</strong><p>{review.reason}</p>{review.gaps.map((gap) => <small key={gap.desc}>{gap.priority} · {gap.desc}</small>)}</div>)}</article></section>

        <section className="evidence-ledger"><article><span className="section-kicker">06 / 证据台账</span><h2>{stream.evidence.length} 条公开材料</h2>{stream.evidence.map((item) => <div className="evidence-row" key={item.id}><span className="mono">{item.id}</span><div><strong>{item.title}</strong><small>{item.source} · L{item.tier}</small></div></div>)}</article><article><span className="section-kicker">07 / 降级与判定</span><h2>系统没有藏起来的限制</h2>{stream.degradations.map((item, index) => <p className="degradation" key={`${index}-${item}`}>{item}</p>)}{stream.decisions.map((item, index) => <p className="decision" key={`${index}-${item}`}>{item}</p>)}{stream.errors.map((item, index) => <p className="error" key={`${index}-${item}`}>{item}</p>)}</article></section>
      </>}
    </main><footer>外文原文优先核验 · 评论仅代表确认帖子的脱敏样本 · 不绕过登录风控 · 不做舆情走向预测 · V2</footer>
  </>
}

export default App
