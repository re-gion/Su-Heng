import { FormEvent, useEffect, useMemo, useState } from 'react'
import {
  createTask,
  TaskCreationError,
  deleteTask,
  getConfig,
  getCommentPluginStatus,
  getCommentCandidates,
  getTopicCandidates,
  expandTopicDiscovery,
  getTaskDetail,
  openCommentLogin,
  clearCommentProfile,
  submitCommentSelection,
  submitTopicSelection,
  stopCommentCollection,
  listTasks,
  pauseTask,
  resumeTask,
  stopTask,
  testConfig,
  updateConfig,
  type BudgetTable,
  type PublicConfig,
  type ProviderQuotaStatus,
  type TaskListItem,
  type CommentPluginStatus,
  type CommentCandidates,
  type TopicCandidates,
  type TaskDetail,
} from './api/client'
import { useTaskStream } from './events/useTaskStream'
import { TaskProgress } from './events/TaskProgress'
import type { ForumItem } from './events/state'
import './styles.css'
import './form.css'
import './suheng.css'
import brandLockup from './assets/brand/suheng-lockup.svg'
import brandSymbol from './assets/brand/suheng-symbol.svg'

const roleLabels: Record<string, string> = { analyst_a: '事实调查', analyst_b: '媒体传播', analyst_c: '历史洞察', moderator: '协作主持', verifier: '证据核验', reporter: '报告撰写', utility: '通用辅助' }
const originLabels: Record<string, string> = { inherit: '继承默认配置', config: '配置页', env: '环境变量', default: '系统默认' }
const budgetLabels: Record<string, string> = { token_limit: '总 token 上限', search_calls: '搜索调用上限', fetch_calls: '原文抓取上限', max_claims: '陈述数量上限', max_evidence_per_claim: '每条陈述最多证据数', max_verify_calls: '证据关系核验上限', outer_rounds: '协作讨论轮数', top_k: '每次搜索结果数', queries_per_round: '每轮查询数', comment_posts: '评论帖子上限', comments_per_post: '每帖评论上限' }
const providerLabels: Record<string, string> = { langsearch: 'LangSearch 搜索', exa: 'Exa 搜索', qianfan: '百度千帆', bocha: '博查搜索', tavily: 'Tavily 搜索', serper: 'Serper 搜索' }
const platformLabels: Record<string, string> = { weibo: '微博', bilibili: '哔哩哔哩', zhihu: '知乎', xiaohongshu: '小红书', douyin: '抖音', kuaishou: '快手', tieba: '百度贴吧' }
const taskStatusLabels: Record<string, string> = { queued: '排队中', running: '进行中', pausing: '正在暂停', paused: '已暂停', stopping: '正在停止', done: '已完成', failed: '未完成' }
const depthLabels: Record<string, string> = { quick: '快速', standard: '标准', deep: '深入' }

const agents = [
  ['fact_investigator', '事实调查', '核对事件本身与官方材料'],
  ['media_propagation', '媒体传播', '梳理采编主体与报道口径'],
  ['history_insight', '历史洞察', '本地历史库优先，搜索回溯补充'],
  ['comment_insight', '评论洞察', '仅分析用户确认帖子的脱敏评论样本'],
] as const

const phaseLabels: Record<string, string> = {
  planning: '制定调查计划', forum: '三路协作调查', searching: '检索公开来源',
  summarizing: '生成证据化陈述', verifying: '逐条交叉核验', reporting: '核验报告内容与交付等级',
  finished: '分析完成', pausing: '正在停到安全点', stopping: '封存证据并出报告', skipped: '已跳过', done: '已完成', failed: '本阶段调用失败', blocked: '调用受限', queued: '等待调度',
  comment_selection: '等待确认高价值帖子', comment_collection: '采集确认帖子的评论', comment_analysis: '分析脱敏评论样本', awaiting_selection: '候选帖子待确认',
  topic_discovery: '发现具体事件', topic_preflight: '核验手工事件来源', topic_selection: '等待选择具体事件', scope_empty: '本轮没有合格材料',
  quality_recovery: '核验后专项补查', partial: '部分完成，可恢复',
}

const forumAgentLabels: Record<string, string> = {
  fact_investigator: '事实调查', media_propagation: '媒体传播', history_insight: '历史洞察',
  comment_insight: '评论洞察', moderator: '主持人',
}
const forumTypeLabels: Record<string, string> = {
  finding: '发现', summary: '调查摘要', question: '提问', review: '评审',
  directive: '定向补查', conflict: '分歧', system: '系统说明',
}

function forumAgentLabel(agent: string): string {
  return forumAgentLabels[agent] ?? agent
}

function legacySummaryParts(content: string): string[] {
  // 旧事件仅存拼接正文。保留分号与原文，只把已有分隔符变成视觉段落。
  return (content.match(/[^；]+；?|；/g) ?? [content]).map((part) => part.trim()).filter(Boolean)
}

function ForumMessageCard({ item }: { item: ForumItem }) {
  const summaryParts = item.type === 'summary' && !item.summaryItems.length ? legacySummaryParts(item.content) : []
  const isRecovery = item.phase === 'quality_recovery'
  return <div data-agent={item.agent} className={`forum-message ${item.type}${isRecovery ? ' quality-recovery' : ''}`}>
    <span className="forum-identity">{isRecovery ? '核验后专项补查' : `第 ${item.round} 轮`} · {forumAgentLabel(item.agent)}{item.type === 'directive' && item.targetAgent ? ` → ${forumAgentLabel(item.targetAgent)}` : ''}</span>
    <strong>{forumTypeLabels[item.type] ?? item.type}</strong>
    <div className="forum-body">
      {item.summaryItems.length > 0
        ? <ul className="forum-points">{item.summaryItems.map((part, index) => <li key={`${part.claimRef}-${index}`}><span>{part.text}</span>{(part.claimRef || part.evidenceRefs.length > 0) && <small>{[part.claimRef, ...part.evidenceRefs].filter(Boolean).join(' · ')}</small>}</li>)}</ul>
        : summaryParts.length > 1
          ? <ul className="forum-points">{summaryParts.map((part, index) => <li key={`${index}-${part}`}>{part}</li>)}</ul>
          : <p>{item.content}</p>}
      {!item.summaryItems.length && item.refs.length > 0 && <small className="forum-refs">关联证据：{item.refs.join(' · ')}</small>}
    </div>
  </div>
}

// 设置页只提交"改动过的"预算字段：留空表示沿用默认，清空某格表示删掉那条覆盖。
// 直接整体覆盖会让用户没重新填写的既有覆盖被静默抹掉。
function mergeBudgetOverrides(base: BudgetTable, draft: Record<string, string>, depths: string[], fields: string[]): BudgetTable {
  const overrides: BudgetTable = Object.fromEntries(Object.entries(base).map(([depth, values]) => [depth, { ...values }]))
  for (const depth of depths) {
    for (const field of fields) {
      const raw = draft[`budget.${depth}.${field}`]
      if (raw === undefined) continue
      const table = overrides[depth] ?? {}
      overrides[depth] = table
      const trimmed = raw.trim()
      if (trimmed === '') delete table[field]
      else if (Number(trimmed) > 0) table[field] = Number(trimmed)
    }
  }
  for (const depth of Object.keys(overrides)) {
    if (!Object.keys(overrides[depth]).length) delete overrides[depth]
  }
  return overrides
}

const quotaPeriodLabels: Record<string, string> = { day: '今日', month: '本月', lifetime: '累计' }
const quotaUnitLabels: Record<string, string> = { calls: '次', credits: '积分', milli_usd: '毫美元', tokens: 'tokens' }
const freeTiers: Record<string, { text: string; url: string }> = {
  langsearch: { text: '$0/月；按账户 RPS、TPM、TPD 限流，实际 token 上限见控制台', url: 'https://docs.langsearch.com/limits/api-limits' },
  exa: { text: '注册赠额按账户显示；每月 $10 credits', url: 'https://exa.ai/pricing' },
  qianfan: { text: '文档参考 50 次/日、1500 次/月；账号权益以控制台为准', url: 'https://cloud.baidu.com/doc/qianfan-api/s/Wmbq4z7e5' },
  bocha: { text: '1000 次免费试用包（3 个月）；另有 1000 次 ¥3.6 体验包（3 个月）。本地保护只按免费试用包计算，领取资格与余额以账号控制台为准', url: 'https://open.bochaai.com/' },
  tavily: { text: '1000 credits/月（基础搜索通常 1 credit）', url: 'https://www.tavily.com/pricing' },
  serper: { text: '2500 次，一次性赠额', url: 'https://serper.dev/' },
  firecrawl: { text: '1000 credits/月；普通单页抓取通常 1 credit', url: 'https://www.firecrawl.dev/pricing' },
}

function quotaDescription(status: ProviderQuotaStatus | undefined): string {
  if (!status?.windows.length) return '未记录本地额度'
  return status.windows.map((window) => {
    const prefix = `${quotaPeriodLabels[window.period] ?? window.period}已记录 ${window.used}`
    const unit = quotaUnitLabels[window.unit] ?? window.unit
    if (window.normal_limit === null) return window.unit === 'milli_usd' ? `${prefix} ${unit}` : `${prefix} ${unit} · 账户限额见控制台`
    const limit = `${window.normal_limit} ${unit}`
    if (window.critical_remaining === 0) return `${prefix}/${limit} · 常规与应急保护线均已到达`
    if (window.normal_remaining === 0) return `${prefix}/${limit} · 常规保护线已到达，应急余量 ${window.critical_remaining ?? 0} ${unit}`
    return `${prefix}/${limit} · 常规余量 ${window.normal_remaining ?? 0} ${unit}`
  }).join('；')
}

function QuotaDetails({ provider, status }: { provider: string; status?: ProviderQuotaStatus }) {
  const tier = freeTiers[provider]
  return <div className="quota-details">
    {tier && <p>免费层级：{tier.text} <a href={tier.url} target="_blank" rel="noreferrer">官方说明</a></p>}
    <p>本机使用（新版本起）：今日 {status?.activity?.day_calls ?? '暂无完整记录'} 次 · 本月 {status?.activity?.month_calls ?? '暂无完整记录'} 次</p>
    <p>本地保护：{quotaDescription(status)}</p>
  </div>
}

function BackToTop({ settings = false }: { settings?: boolean }) {
  return <button
    type="button"
    className={`back-to-top${settings ? ' back-to-top-settings' : ''}`}
    aria-label="返回顶部"
    onClick={() => window.scrollTo({
      top: 0,
      behavior: window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
    })}
  ><span aria-hidden="true">↑</span> 返回顶部</button>
}

function Settings({ onClose }: { onClose: () => void }) {
  const [config, setConfig] = useState<PublicConfig | null>(null)
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [notice, setNotice] = useState('')
  const [fetchOrder, setFetchOrder] = useState<string[]>([])
  const [commentStatus, setCommentStatus] = useState<CommentPluginStatus | null>(null)
  const [resetBudgets, setResetBudgets] = useState(false)
  useEffect(() => { void getConfig().then((value) => { setConfig(value); setFetchOrder(value.fetch?.provider_order ?? ['builtin', 'firecrawl']) }).catch((error) => setNotice(String(error))) }, [])
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
    const fetchKeys = Object.fromEntries(Object.keys(currentConfig.fetch?.keys ?? {}).flatMap((name) => draft[`fetch.${name}`] ? [[name, draft[`fetch.${name}`]]] : []))
    const budgetTouched = resetBudgets || Object.keys(draft).some((key) => key.startsWith('budget.'))
    const budgetOverrides = resetBudgets ? {} : mergeBudgetOverrides(currentConfig.budget.overrides, draft, currentConfig.budget.depths, currentConfig.budget.fields)
    try {
      const updated = await updateConfig({ llm: { roles: rolePayload }, search: { keys: searchKeys }, fetch: { keys: fetchKeys, provider_order: fetchOrder }, comments: { enabled: draft['comments.enabled'] ? draft['comments.enabled'] === 'true' : currentConfig.comments.enabled }, ...(budgetTouched ? { budget: { overrides: budgetOverrides } } : {}) })
      setConfig(updated); setFetchOrder(updated.fetch.provider_order); setDraft({}); setResetBudgets(false); setNotice('已保存。新配置会用于下一次任务。')
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
    <div className="settings-head"><div><img className="settings-brand" src={brandLockup} alt="溯衡" width="138" height="64" /><span className="section-kicker">系统配置</span><h1>配置调查服务</h1><p>设置页覆盖 `.env`，密钥只显示脱敏值，不写回文件。</p></div><button type="button" onClick={onClose}>返回调查台</button></div>
    <form className="settings-form" onSubmit={save}>
      <section><h2>模型角色配置</h2><p className="muted">留空继续继承当前有效值；核验器与生成模型异构时，交叉核验更有意义。</p>
        <div className="role-config-grid">{roles.map(([role, value]) => <article key={role}>
          <header><div><strong>{roleLabels[role] ?? role}</strong><small className="role-key">{role}</small></div><button type="button" onClick={() => void test('llm', role)}>测试</button></header>
          <label>接口密钥 · API Key<input type="password" value={draft[`${role}.api_key`] ?? ''} onChange={(event) => set(`${role}.api_key`, event.target.value)} placeholder={value.effective.api_key ?? '未配置'} /></label>
          <label>接口地址 · Base URL<input value={draft[`${role}.base_url`] ?? ''} onChange={(event) => set(`${role}.base_url`, event.target.value)} placeholder={value.effective.base_url} /></label>
          <label>模型名称 · Model<input value={draft[`${role}.model`] ?? ''} onChange={(event) => set(`${role}.model`, event.target.value)} placeholder={value.effective.model} /></label>
          <small>配置来源：{Object.entries(value.source).map(([key, source]) => `${({ api_key: '密钥', base_url: '地址', model: '模型' } as Record<string, string>)[key] ?? key}：${originLabels[source] ?? source}`).join('；')}{role === 'analyst_b' && <span className="role-usage">评论逐条分类也使用此角色；主题综合使用报告撰写，审查使用证据核验。</span>}</small>
        </article>)}</div>
      </section>
      <section><h2>搜索路由与本地额度</h2><p className="muted">中文查询依次尝试 LangSearch → Exa → 千帆 → 博查，其他语言依次尝试 Exa → Tavily → Serper。结果为空或与事件不相关时会继续换源；中文主调查若 LangSearch 只找到同一发布主体的材料，还会用 Exa 补充并去重。千帆与 Tavily 每任务最多 20 次，博查与 Serper 每任务最多 10 次。本机计数从此版本启用后开始记录，包含已预留但上游失败的调用；免费额度是公开参考值，实际账户余额请以各家控制台为准。</p><div className="search-config-grid">{Object.entries(currentConfig.search.keys).map(([name, masked]) => <div className="provider-config" key={name}><label><span>{providerLabels[name] ?? name}<button type="button" onClick={() => void test('search', name)}>测试</button></span><input type="password" value={draft[`search.${name}`] ?? ''} onChange={(event) => set(`search.${name}`, event.target.value)} placeholder={masked ?? '未配置'} /></label><QuotaDetails provider={name} status={currentConfig.search.quota?.[name]} /></div>)}</div></section>
      <section className="fetch-settings"><h2>原文获取</h2><p className="muted">优先使用搜索服务返回的足量正文；需要原文快照或确认历史材料日期时，使用内置网页抓取。只有高等级、已入选的关键证据页遇到挑战页、空正文、PDF 解析失败或可重试错误耗尽时才回退 Firecrawl Cloud，每个任务最多 5 页。</p><div className="provider-order" aria-label="原文取得优先级">{fetchOrder.map((name, index) => <span key={name}><strong>{index + 1}. {name === 'builtin' ? '内置网页抓取' : 'Firecrawl 补充抓取'}</strong></span>)}</div>{Object.entries(currentConfig.fetch?.keys ?? {}).map(([name, masked]) => <div className="provider-config" key={name}><label>Firecrawl 接口密钥 · API Key<input type="password" value={draft[`fetch.${name}`] ?? ''} onChange={(event) => set(`fetch.${name}`, event.target.value)} placeholder={masked ?? '未配置'} /></label><QuotaDetails provider={name} status={currentConfig.fetch.quota?.[name]} /></div>)}</section>
      <section><h2>调查深度预算</h2><p className="muted">留空沿用当前有效值，清空某一格即删除该条覆盖（取值来源：{currentConfig.budget.source === 'config' ? '配置表' : '默认值'}）。核验额度按每条陈述实际引用的证据数消耗；额度不足时，尚未完成核验的陈述会保留待核验状态。<button type="button" className="quiet" onClick={() => { setResetBudgets(true); setDraft((current) => Object.fromEntries(Object.entries(current).filter(([key]) => !key.startsWith('budget.')))) }}>全部恢复默认</button></p>
        <div className="budget-grid">{currentConfig.budget.depths.map((depth) => <article key={depth}>
          <header><strong>{depthLabels[depth] ?? depth}</strong><small>{Object.keys(currentConfig.budget.overrides[depth] ?? {}).length ? `已覆盖 ${Object.keys(currentConfig.budget.overrides[depth]).length} 项` : '默认'}</small></header>
          {currentConfig.budget.fields.map((field) => <label key={field}>{budgetLabels[field] ?? field}<input type="number" min={1} value={draft[`budget.${depth}.${field}`] ?? ''} onChange={(event) => set(`budget.${depth}.${field}`, event.target.value)} placeholder={String(currentConfig.budget.effective[depth]?.[field] ?? '')} /></label>)}
        </article>)}</div>
      </section>
      <section><h2>登录态评论插件</h2><p className="muted">仅限本机使用。登录信息留在专用浏览器 profile，不写入数据库或报告。</p><label className="toggle-row"><input type="checkbox" checked={(draft['comments.enabled'] ? draft['comments.enabled'] === 'true' : currentConfig.comments.enabled)} disabled={!commentStatus?.available} onChange={(event) => set('comments.enabled', String(event.target.checked))} />启用事件确认后的智能评论选帖与采集</label><p className="muted">{commentStatus?.risk_notice}</p><div className="platform-login-grid">{commentStatus?.platforms.map((item) => <article key={item.platform}><strong>{platformLabels[item.platform] ?? item.platform}</strong><small>{item.browser_open ? '登录浏览器已打开' : item.profile_present ? '已有本地 profile' : '尚未登录'}</small><button type="button" disabled={!commentStatus.available || !currentConfig.comments.enabled} onClick={() => void openCommentLogin(item.platform).then(() => setNotice(`${item.platform} 登录浏览器已打开`)).catch((error) => setNotice(String(error)))}>打开登录浏览器</button><button type="button" className="quiet" disabled={!item.profile_present} onClick={() => { if (confirm(`确定清除 ${item.platform} 的专用浏览器登录数据？`)) void clearCommentProfile(item.platform).then(() => getCommentPluginStatus().then(setCommentStatus)) }}>清除登录</button></article>)}</div></section>
      <div className="settings-save"><button>保存配置</button><span>{notice}</span></div>
    </form>
    <BackToTop settings />
  </main>
}

const sourceRoleLabels: Record<string, string> = {
  authority: '权威机构', party: '当事方', independent: '独立媒体', aggregator: '聚合来源', unknown: '角色待确认',
}

function TopicSelectionPanel({ data, selectedTopic, manualTopic, onSelect, onManual, onSubmit, onExpand }: {
  data: TopicCandidates
  selectedTopic: string
  manualTopic: string
  onSelect: (id: string) => void
  onManual: (value: string) => void
  onSubmit: (payload: { candidate_id?: string; event_query?: string; force?: boolean }) => void
  onExpand: () => void
}) {
  const manualValue = manualTopic.trim()
  const failedManual = data.manual_preflight?.status === 'unverified' && data.manual_preflight.query === manualValue
  const range = data.effective_time_range
  return <section className="topic-selection">
    <span className="section-kicker">范围确认 / 事件证据簇</span>
    <h2>先选定这次要深入调查的事件</h2>
    <p>“{data.original_query}”是宽泛主题。候选按事件聚合多个来源；系统不会把不同事件混成一份报告，也不会替你默认勾选。</p>
    {range && <p className="topic-range">检索窗口 {range.date_from} — {range.date_to}{data.used_default_time_range ? '（未指定日期，默认近 12 个月）' : ''}</p>}
    {data.provider_coverage?.limited && <p className="coverage-warning">检索覆盖受限：{data.provider_coverage.message ?? '当前不足两条独立检索路径，候选不能宣称完整。'}</p>}
    {data.items.length > 0 ? <div className="topic-card-list">{data.items.map((item, index) => <label key={item.id} className={`topic-card ${selectedTopic === item.id ? 'selected' : ''}`}>
      <input type="radio" name="topic-candidate" checked={selectedTopic === item.id} onChange={() => onSelect(item.id)} />
      <span className="topic-card-body">
        <span className="topic-card-meta"><span className={`confidence-badge ${item.confidence}`}>{item.confidence_label}</span>{index === 0 && <span>证据排序第 1</span>}<span>{item.source_count} 个来源</span><span>评分 {item.score.toFixed(1)}</span></span>
        <strong>{item.title}</strong>
        <small>{item.date_from ? `${item.date_from}${item.date_to && item.date_to !== item.date_from ? ` — ${item.date_to}` : ''}` : item.date_status}</small>
        <p>{item.summary}</p>
        <div className="topic-reasons">{item.reasons.map((reason) => <span key={reason}>✓ {reason}</span>)}</div>
        {item.gaps.length > 0 && <p className="topic-gaps">待补证：{item.gaps.join('；')}</p>}
        <details onClick={(event) => event.stopPropagation()}><summary>查看 {item.sources.length} 条候选来源</summary><div className="topic-sources">{item.sources.map((source) => <a key={source.url} href={source.url} target="_blank" rel="noreferrer"><strong>{source.source_name}</strong><span>{sourceRoleLabels[source.role] ?? source.role} · {source.provider} · {source.published_at?.slice(0, 10) ?? '日期待核'}</span><small>{source.title}</small></a>)}</div></details>
      </span>
    </label>)}</div> : <><p className="degradation">初始检索和一次受限恢复均未形成可靠事件簇。系统没有用导航页、例行通知或无关结果凑数；可查看诊断后补充更具体的事件。</p>{data.used_default_time_range && <button className="quiet" onClick={onExpand}>把时间窗口扩展到近 3 年后重试</button>}</>}
    {data.attempts.length > 0 && <details className="topic-diagnostics"><summary>查看检索诊断（{data.attempts.length} 次尝试）</summary>{data.attempts.map((attempt, index) => <p key={`${attempt.round}-${attempt.provider}-${index}`}><strong>{attempt.round === 'recovery' ? '恢复检索' : attempt.round === 'manual_preflight' ? '手工预检' : '初始检索'} · {attempt.provider}</strong><span>{attempt.query}</span><small>{attempt.status} · 原始 {attempt.raw_hits} · 接受 {attempt.accepted_hits}{Object.keys(attempt.rejected).length ? ` · 排除 ${Object.entries(attempt.rejected).map(([reason, count]) => `${reason} ${count}`).join(' / ')}` : ''}{attempt.error ? ` · ${attempt.error}` : ''}</small></p>)}</details>}
    {data.manual_preflight?.status === 'unverified' && <p className="degradation">{data.manual_preflight.message ?? '该手工事件尚未找到可确认的公开来源。'}你可以修改名称重新验证，或明确以线索继续；后者只生成检索诊断，不生成完整专报。</p>}
    <textarea value={manualTopic} onChange={(event) => onManual(event.target.value)} placeholder="候选不准确时，填写具体事件、争议点或关键行为" aria-label="具体事件" />
    <div className="candidate-actions"><button disabled={!selectedTopic && !manualValue} onClick={() => onSubmit(selectedTopic ? { candidate_id: selectedTopic } : { event_query: manualValue })}>{selectedTopic ? '确认事件并开始深入调查' : '验证事件来源'}</button>{failedManual && <button className="quiet" onClick={() => onSubmit({ event_query: manualValue, force: true })}>仍以线索继续（只生成检索诊断）</button>}</div>
  </section>
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
  const [topicCandidates, setTopicCandidates] = useState<TopicCandidates | null>(null)
  const [taskDetail, setTaskDetail] = useState<TaskDetail | null>(null)
  const [selectedCandidates, setSelectedCandidates] = useState<string[]>([])
  const [supplementalCommentUrls, setSupplementalCommentUrls] = useState('')
  const [selectedTopic, setSelectedTopic] = useState('')
  const [manualTopic, setManualTopic] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [formError, setFormError] = useState('')
  const [pendingScope, setPendingScope] = useState<{ key: string; label: string } | null>(null)
  const [tasks, setTasks] = useState<TaskListItem[]>([])
  const [appCapabilities, setAppCapabilities] = useState<CommentPluginStatus | null>(null)
  const stream = useTaskStream(taskId, streamRevision)
  const draftKey = JSON.stringify([query, note, timeFrom, timeTo, depth, sourceScope, sourceLanguages, commentMode, commentUrls])
  useEffect(() => { void listTasks().then(setTasks).catch(() => setTasks([])) }, [taskId, stream.status])
  useEffect(() => { void getCommentPluginStatus().then(setAppCapabilities).catch(() => setAppCapabilities(null)) }, [])
  useEffect(() => {
    if (!taskId) {
      setTaskDetail(null)
      return
    }
    let active = true
    void getTaskDetail(taskId).then(value => { if (active) setTaskDetail(value) }).catch(() => { if (active) setTaskDetail(null) })
    return () => { active = false }
  }, [taskId, streamRevision, stream.status, stream.phase, stream.reportUrl])
  useEffect(() => { if (taskId && stream.phase === 'comment_selection') void getCommentCandidates(taskId).then((value) => { setCommentCandidates(value); setSelectedCandidates(value.items.slice(0, value.budgets.posts).map((item) => item.id)) }).catch(() => setCommentCandidates(null)); else setCommentCandidates(null) }, [taskId, stream.phase])
  useEffect(() => {
    const phase = taskDetail?.phase ?? stream.phase
    if (taskId && phase === 'topic_selection') void getTopicCandidates(taskId).then((value) => { setTopicCandidates(value); setSelectedTopic(''); setManualTopic(value.manual_preflight?.status === 'unverified' ? value.manual_preflight.query ?? '' : '') }).catch(() => setTopicCandidates(null))
    else setTopicCandidates(null)
  }, [taskId, taskDetail?.phase, stream.phase])

  function selectTask(id: string) { history.replaceState(null, '', `?task=${encodeURIComponent(id)}`); setTaskId(id); setStreamRevision((value) => value + 1) }
  function clearTask() { history.replaceState(null, '', location.pathname); setTaskId(null); setStreamRevision((value) => value + 1) }
  async function action(work: () => Promise<unknown>) { setFormError(''); try { await work() } catch (error) { setFormError(error instanceof Error ? error.message : '操作失败') } }
  async function createCurrentTask(investigationScope: 'general' | 'institution' | 'public_event') {
    const task = await createTask(query.trim(), depth, note.trim(), timeFrom, timeTo, { sourceScope, sourceLanguages: sourceLanguages.split(',').map((item) => item.trim()).filter(Boolean).slice(0, 3), commentMode, commentUrls: commentUrls.split(/\r?\n/).map((item) => item.trim()).filter(Boolean), investigationScope })
    setPendingScope(null)
    selectTask(task.task_id)
  }
  async function submit(event: FormEvent) {
    event.preventDefault(); if (!query.trim() || submitting) return; setSubmitting(true); setFormError(''); setPendingScope(null)
    try { await createCurrentTask('general') }
    catch (error) {
      if (error instanceof TaskCreationError && error.code === 'SCOPE_CONFIRMATION_REQUIRED') setPendingScope({ key: draftKey, label: error.scopeLabel ?? '调查公开事件全貌，普通个人匿名化，不挖掘私人信息。' })
      else setFormError(error instanceof Error ? error.message : '创建任务失败')
    }
    finally { setSubmitting(false) }
  }
  async function confirmInstitutionScope() {
    if (!pendingScope || pendingScope.key !== draftKey || submitting) return
    setSubmitting(true); setFormError('')
    try { await createCurrentTask('public_event') }
    catch (error) { setFormError(error instanceof Error ? error.message : '创建任务失败') }
    finally { setSubmitting(false) }
  }
  if (screen === 'settings') return <Settings onClose={() => setScreen('desk')} />
  const visibleAgents = agents.filter(([key]) => key !== 'comment_insight' || commentMode !== 'off' || stream.phase.startsWith('comment_') || Boolean(stream.agents.comment_insight))
  const hasCommentCandidates = Boolean(commentCandidates?.items.length)
  const allowsSupplementalCommentUrls = ['manual', 'hybrid'].includes(commentMode)
  const detailPhase = taskDetail?.phase ?? stream.phase
  const detailStatus = taskDetail?.status ?? stream.status
  const endLabels: Record<string, string> = { approved: '主持人批准结束讨论', round_limit: '讨论达到轮次上限', budget_exhausted: '预算不足', verification_reserve: '调查达到阶段预算上限，额度留给核验与报告', no_progress: '连续两次补查无新增有效结果', user_stop: '用户停止调查', core_complete: '核心内容评估完成', unknown: '旧任务未记录结束原因' }
  const releaseLabels: Record<string, string> = { full_report: '完整舆情专报', evidence_brief: '证据简报', retrieval_diagnostic: '检索诊断' }
  const ending = taskDetail?.recovery?.end_reason ?? taskDetail?.investigation_outcome?.end_reason
  const reportLevel = taskDetail?.release_label ? `报告等级：${releaseLabels[taskDetail.release_label] ?? taskDetail.release_label}。` : '报告等级待生成。'
  const canCommentSelection = detailPhase === 'comment_selection' && detailStatus === 'paused'
  const canTopicSelection = detailPhase === 'topic_selection' && detailStatus === 'paused'
  const canStopTask = ['running', 'pausing', 'paused'].includes(detailStatus)
  const forumRounds = stream.forum.filter((item) => item.phase !== 'quality_recovery')
  const qualityRecoveryMessages = stream.forum.filter((item) => item.phase === 'quality_recovery')

  return <>
    <header className="masthead"><div className="brand-line"><a className="brand-home" href="/" aria-label="溯衡首页"><img src={brandLockup} alt="溯衡" width="138" height="64" /></a><span>公开材料 · 证据回查 · 审慎研判</span><button className="text-button" disabled={appCapabilities?.demo_mode} onClick={() => setScreen('settings')}>{appCapabilities?.demo_mode ? '演示站只读' : '系统配置 ↗'}</button></div>
      <div className="hero-copy"><h1>舆情调查与研判</h1><p className="hero-note">从事件事实、媒体报道到评论样本，逐项核对来源，保留判断依据。</p></div>
    </header>
    <main>
      <section className="query-panel"><span className="section-kicker">新建调查</span><h2>这次要核查什么？</h2><form onSubmit={submit}>
        <div className="query-fields"><input value={query} onChange={(event) => setQuery(event.target.value)} maxLength={200} placeholder="例如：某企业产品召回事件" aria-label="事件名称" /><input value={note} onChange={(event) => setNote(event.target.value)} placeholder="可选：特别关注点" aria-label="特别关注点" /><label className="date-field"><span>起始日期</span><input type="date" value={timeFrom} max={timeTo || undefined} onChange={(event) => setTimeFrom(event.target.value)} /></label><label className="date-field"><span>结束日期</span><input type="date" value={timeTo} min={timeFrom || undefined} onChange={(event) => setTimeTo(event.target.value)} /></label></div>
        <div className="v2-options"><label>调查深度<select value={depth} onChange={(event) => setDepth(event.target.value)} aria-label="调查深度"><option value="quick">快速</option><option value="standard">标准</option><option value="deep">深入</option></select></label><label>信源范围<select value={sourceScope} onChange={(event) => setSourceScope(event.target.value as typeof sourceScope)}><option value="auto">自动判断</option><option value="domestic">国内优先</option><option value="global">境外扩展</option></select></label><label>信源语言<input value={sourceLanguages} onChange={(event) => setSourceLanguages(event.target.value)} placeholder="zh,en" /></label><label>评论深挖<select value={commentMode} onChange={(event) => setCommentMode(event.target.value as typeof commentMode)}><option value="off">关闭</option><option value="smart">事件确认后智能找帖</option><option value="manual">事件确认后指定 URL</option><option value="hybrid">事件确认后混合</option></select></label></div><p className="comment-mode-note">评论选帖只在具体事件确认后运行，不参与上面的事件发现。</p>{['manual', 'hybrid'].includes(commentMode) && <textarea value={commentUrls} onChange={(event) => setCommentUrls(event.target.value)} placeholder="每行一个微博/B站/知乎/小红书/抖音/快手/贴吧帖子 URL" aria-label="指定帖子 URL" />}
        <button disabled={submitting || !query.trim()}>{submitting ? '正在建立协作任务…' : '启动三路调查'}</button>
      </form>{pendingScope?.key === draftKey && <div className="scope-confirmation" role="alert"><h3>确认调查范围</h3><p>{pendingScope.label}</p><p>确认后，调查、评论和报告都按此范围处理；明确不符合范围的内容会被排除；审查未完成会单独说明。</p><div><button type="button" disabled={submitting} onClick={() => void confirmInstitutionScope()}>按公开事件范围继续</button><button type="button" className="quiet" onClick={() => setPendingScope(null)}>返回修改</button></div></div>}{formError && <p className="error">{formError}</p>}</section>


      {tasks.length > 0 && <section className="task-shelf"><span className="section-kicker">历史 / 任务台账</span><div className="task-list">{tasks.map((item) => <article key={item.task_id} className={item.task_id === taskId ? 'active' : ''}><button className="task-title" onClick={() => selectTask(item.task_id)}><strong>{item.resolved_event_query ?? item.event_query}</strong><small>{taskStatusLabels[item.status] ?? item.status} · {item.task_id}</small></button><div className="task-actions">{item.topic_selection_required && <button onClick={() => selectTask(item.task_id)}>选择事件</button>}{item.comment_selection_required && <button onClick={() => selectTask(item.task_id)}>确认评论</button>}{item.resumable && <button onClick={() => void action(async () => { await resumeTask(item.task_id); selectTask(item.task_id) })}>续跑</button>}{!['running', 'pausing', 'stopping'].includes(item.status) && <button className="quiet" onClick={() => void action(async () => { await deleteTask(item.task_id); if (item.task_id === taskId) clearTask(); setTasks(await listTasks()) })}>删除</button>}</div></article>)}</div></section>}

      {taskId && <>
        <section className="run-console"><div className="run-status"><span className={`status-dot ${stream.status}`} /><div><span className="section-kicker">02 / {taskStatusLabels[stream.status] ?? stream.status}</span><h2>{phaseLabels[stream.phase] ?? stream.phase}</h2><p className="mono">{taskId} · EVENT {String(stream.lastSeq).padStart(3, '0')}</p></div><div className="run-actions">{stream.status === 'running' && canStopTask && <button onClick={() => void action(() => pauseTask(taskId))}>暂停</button>}{stream.phase === 'comment_collection' && <button onClick={() => void action(() => stopCommentCollection(taskId))}>停止评论采集</button>}{canStopTask && <button onClick={() => void action(async () => { await stopTask(taskId); selectTask(taskId) })}>停止并出报告</button>}{stream.reportUrl && <a className="report-link" href={stream.reportUrl}>查看调查报告 →</a>}</div></div>
          <TaskProgress key={taskId} taskId={taskId} stream={stream} revision={streamRevision} />
          {(ending || taskDetail?.release_label) && <p className="investigation-outcome">{ending ? `${endLabels[ending] ?? ending}。` : ''}{reportLevel}{taskDetail?.recovery?.round ? ` 已执行 ${taskDetail.recovery.round} 次专项补查。` : ''}</p>}
        </section>

        {canTopicSelection && topicCandidates && <TopicSelectionPanel data={topicCandidates} selectedTopic={selectedTopic} manualTopic={manualTopic} onSelect={(id) => { setSelectedTopic(id); setManualTopic('') }} onManual={(value) => { setManualTopic(value); setSelectedTopic('') }} onSubmit={(payload) => void action(async () => { await submitTopicSelection(taskId, payload); selectTask(taskId) })} onExpand={() => void action(async () => { await expandTopicDiscovery(taskId); selectTask(taskId) })} />}

        <section className="agent-rail"><span className="section-kicker">03 / 并行调查席</span><div className="agent-panels">{visibleAgents.map(([key, label, description]) => <article key={key} data-agent={key}><div className="agent-index">{key === 'fact_investigator' ? 'A' : key === 'media_propagation' ? 'B' : key === 'history_insight' ? 'C' : 'D'}</div><h3>{label}</h3><p>{description}</p><strong>{phaseLabels[stream.agents[key]] ?? stream.agents[key] ?? '等待调度'}</strong></article>)}</div></section>

        {canCommentSelection && commentCandidates && <section className="comment-candidates"><span className="section-kicker">V2 / 评论候选确认</span><h2>选择值得进入的评论区</h2><p>最多 {commentCandidates.budgets.posts} 帖，每帖 {commentCandidates.budgets.comments_per_post} 条。确认前系统不会访问登录态内容。</p>{hasCommentCandidates ? <><div>{commentCandidates.items.map((item) => <label key={item.id} className="candidate-card"><input type="checkbox" checked={selectedCandidates.includes(item.id)} disabled={!selectedCandidates.includes(item.id) && selectedCandidates.length >= commentCandidates.budgets.posts} onChange={(event) => setSelectedCandidates((current) => event.target.checked ? [...current, item.id] : current.filter((id) => id !== item.id))} /><span><strong>{item.platform} · {item.title}</strong><small>评分 {item.score.toFixed(1)} · {item.reasons.join(' / ')} · {item.login_profile_present ? '已有登录 profile' : '需先登录'}</small><a href={item.url} target="_blank" rel="noreferrer">查看原帖</a></span></label>)}</div>{allowsSupplementalCommentUrls && <textarea value={supplementalCommentUrls} onChange={(event) => setSupplementalCommentUrls(event.target.value)} placeholder="可选：每行补充一个帖子 URL，确认时一并采集" aria-label="补充帖子 URL" />}<div className="candidate-actions"><button disabled={!selectedCandidates.length && !(allowsSupplementalCommentUrls && supplementalCommentUrls.trim())} onClick={() => void action(async () => { await submitCommentSelection(taskId, { action: 'approve', candidate_ids: selectedCandidates, urls: supplementalCommentUrls.split(/\r?\n/).map((item) => item.trim()).filter(Boolean) }); selectTask(taskId) })}>确认并采集</button><button className="quiet" onClick={() => void action(async () => { await submitCommentSelection(taskId, { action: 'skip' }); selectTask(taskId) })}>跳过评论，继续报告</button></div></> : <><p className="muted">系统没有找到达到相关度门槛的帖子。下面保留了逐平台结果；请补充帖子 URL，或明确跳过评论洞察。</p>{commentCandidates.discovery_attempts.length > 0 && <div className="discovery-diagnostics">{commentCandidates.discovery_attempts.map((item, index) => <p key={`${item.platform}-${index}`}><strong>{platformLabels[item.platform] ?? item.platform}</strong> · {item.status === 'found' ? `发现 ${item.count} 条` : item.status === 'empty' ? '未发现匹配帖子' : '发现失败'}{item.error ? ` · ${item.error}` : ''}</p>)}</div>}<textarea value={supplementalCommentUrls} onChange={(event) => setSupplementalCommentUrls(event.target.value)} placeholder="每行补充一个帖子 URL" aria-label="补充帖子 URL" /><div className="candidate-actions"><button disabled={!supplementalCommentUrls.trim()} onClick={() => void action(async () => { await submitCommentSelection(taskId, { action: 'approve', urls: supplementalCommentUrls.split(/\r?\n/).map((item) => item.trim()).filter(Boolean) }); selectTask(taskId) })}>使用补充 URL 并采集</button><button className="quiet" onClick={() => void action(async () => { await submitCommentSelection(taskId, { action: 'skip' }); selectTask(taskId) })}>明确跳过评论，继续报告</button></div></>}</section>}

        <section className="collaboration-grid"><article className="forum-board"><span className="section-kicker">04 / 论坛黑板</span><h2>调查与主持人评审记录</h2>{stream.forum.length === 0 ? <p className="muted">等待各调查席发言…</p> : <><div aria-label="论坛讨论轮次">{Array.from(new Set(forumRounds.map(item => item.round))).map(round => <section className="forum-round" key={round}><h3>第 {round} 轮讨论</h3>{forumRounds.filter(item => item.round === round).map((item, index) => <ForumMessageCard item={item} key={`${index}-${item.content}`} />)}</section>)}</div>{qualityRecoveryMessages.length > 0 && <section className="quality-recovery-section" aria-label="核验后专项补查"><h3>核验后专项补查</h3><p>论坛已结束；以下内容由核验发现的具体缺口触发，不属于新的主持人讨论轮次。</p>{qualityRecoveryMessages.map((item, index) => <ForumMessageCard item={item} key={`${index}-${item.content}`} />)}</section>}</>}</article>
          <article className="host-board"><span className="section-kicker">05 / 主持人评审</span><h2>评审结论与待解决项</h2>{stream.hostReviews.length === 0 ? <p className="muted">首轮调查汇合后开始评审。</p> : stream.hostReviews.map((review, index) => <div className="host-review" key={`${index}-${review.reason}`}><strong>{review.release ? '主持人批准结束' : '本轮未获批准'}</strong><p>{review.reason}</p>{review.gaps.map((gap) => <small key={gap.desc}>{({ high: '高优先级', medium: '中优先级', low: '低优先级' } as Record<string, string>)[gap.priority ?? ''] ?? gap.priority} · {gap.desc}</small>)}</div>)}</article></section>

        <section className="evidence-ledger"><article><span className="section-kicker">06 / 证据台账</span><h2>{stream.evidence.length} 条公开材料</h2>{stream.evidence.map((item) => <div className="evidence-row" key={item.id}><span className="mono">{item.id}</span><div><strong>{item.title}</strong><small>{item.source} · L{item.tier}</small></div></div>)}</article><article><span className="section-kicker">07 / 检索路径与判定</span><h2>调查状态与限制说明</h2>{stream.degradations.map((item, index) => <p className="degradation" key={`${index}-${item}`}>{item}</p>)}{stream.decisions.map((item, index) => <p className="decision" key={`${index}-${item}`}>{item}</p>)}{stream.errors.map((item, index) => <p className="error" key={`${index}-${item}`}>{item}</p>)}</article></section>
      </>}
    </main><footer><img className="footer-brand" src={brandSymbol} alt="" width="28" height="28" />溯衡 · 舆情调查与研判　｜　结论可回查来源，评论仅代表已采集样本</footer><BackToTop />
  </>
}

export default App
