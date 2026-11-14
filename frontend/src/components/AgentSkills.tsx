import { useEffect, useMemo, useState } from 'react'
import { getAgentCatalog, type AgentCapability, type AgentCatalog } from '../api/client'
import type { SkillActivity } from '../events/state'
import { activityStatus, groupNotice, groupSkillRecords, modeLabel, outcomeExplanation, selectionExplanation, skillRecordStats, stepLabel, type SkillUsageGroup } from './skillPresentation'
import './agent-skills.css'

const roleOrder = ['utility', 'analyst_a', 'analyst_b', 'analyst_c', 'comment_analyst', 'moderator', 'verifier', 'reporter']

export function useAgentCatalog() {
  const [catalog, setCatalog] = useState<AgentCatalog | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    void getAgentCatalog(controller.signal).then(value => {
      if (!controller.signal.aborted) setCatalog(value)
    }).catch(reason => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '无法读取技能目录')
    })
    return () => controller.abort()
  }, [])
  return { catalog, error }
}

export function AgentSkillSummary({ agent }: { agent?: AgentCapability }) {
  if (!agent) return <p className="muted">技能定义尚未读取，运行能力以实际记录为准。</p>
  const main = agent.skills.find(skill => skill.id === agent.main_skill)
  return <div className="agent-method-summary">
    <p><span className="skill-kind">主技能</span><strong>{main?.label ?? '主技能不可用'}</strong></p>
    <div className="skill-tags">{agent.skills.filter(skill => !skill.shared && skill.id !== agent.main_skill).map(skill => <span key={skill.id}>{skill.label}</span>)}</div>
    <details className="skill-methods"><summary>查看 {agent.skills.length} 项专项与共享技能</summary>
      <p className="muted">{agent.selection_mode === 'model' ? '规划与反思时由模型在允许的技能中选用，整理材料时沿用前序技能。' : '程序按工作阶段匹配技能；不表示模型自主选用。'}技能是可复用的方法说明包，内容只读。</p>
      {agent.skills.map(skill => <section key={skill.id} className="skill-method">
        <h4>{skill.label}<small>{skill.shared ? '共享' : '专项'} · v{skill.version}</small></h4>
        <p>{skill.description}</p><p><strong>预期产物：</strong>{skill.output}</p>
        <pre>{skill.body}</pre><details className="skill-trace"><summary>查看技能标识与文件指纹</summary><p>技能标识：{skill.id}</p><p>技能文件指纹：{skill.digest}</p><p>指纹对应已加载的技能文件（含元数据和正文），不是报告内容。</p></details>
      </section>)}
    </details>
  </div>
}

function SkillUsageRecord({ item, finished }: { item: SkillActivity; finished: boolean }) {
  const explanation = selectionExplanation(item)
  return <details className="skill-activity">
    <summary><strong>{stepLabel(item)}</strong><span>{activityStatus(item, finished)}</span></summary>
    <p><strong>{explanation.label}：</strong>{explanation.text}</p>
    <p>{modeLabel(item)} · 技能版本 v{item.version || '未记录'}</p>
    <p>{outcomeExplanation(item, finished)}</p>
    {item.claim_refs.length > 0 && <p>所属阶段的陈述：{item.claim_refs.join('、')}</p>}
    {item.evidence_refs.length > 0 && <p>所属阶段的证据：{item.evidence_refs.map(ref => <a key={ref} href={`#evidence-${ref}`}>{ref} </a>)}</p>}
    {item.report_id && <p><a href={`/api/reports/${encodeURIComponent(item.report_id)}/html?view=full`} target="_blank" rel="noreferrer">查看所属阶段报告</a></p>}
    <details className="skill-trace"><summary>查看追溯信息</summary><dl>
      <dt>技能标识</dt><dd>{item.skill_id}<small>标识所使用的技能，同一技能可以用于多个处理步骤。</small></dd>
      <dt>技能文件指纹</dt><dd>{item.digest || '未记录'}<small>对应已加载技能文件的内容（含元数据和正文）。内容相同，指纹相同；不是报告指纹。</small></dd>
      <dt>处理记录编号</dt><dd>{item.invocation_id}<small>每次新处理生成独立编号；同一记录更新状态或刷新回放时保持不变。</small></dd>
      <dt>内部阶段代码</dt><dd>{item.stage || '未记录'}</dd>
    </dl></details>
  </details>
}

function SkillRecordGroup({ group, agentLabel, finished, expanded }: {
  group: SkillUsageGroup; agentLabel: string; finished: boolean; expanded: boolean
}) {
  const [limit, setLimit] = useState(3)
  const item = group.records[0]
  const notice = groupNotice(group.records, finished)
  return <details className="skill-group" open={expanded}>
    <summary><strong>{item.label}</strong><span>{agentLabel} · {group.records.length} 条处理记录 · v{item.version || '未记录'}</span>{notice && <span className="skill-group-notice">{notice}</span>}</summary>
    <p className="muted">{item.version && item.digest ? '以下记录使用同一角色、同一版本和同一文件指纹的技能；每条表示一次处理，不代表一个独立技能或一次完整业务任务。' : '这条记录缺少版本或文件指纹，单独展示；无法确认是否与其他记录使用相同的技能文件。'}</p>
    {group.records.slice(-limit).reverse().map(record => <SkillUsageRecord key={record.invocation_id} item={record} finished={finished} />)}
    {group.records.length > limit && <button type="button" onClick={() => setLimit(value => value + 12)}>展开更早的 {group.records.length - limit} 条处理记录</button>}
  </details>
}

export function AgentSkillRoster({ activities = [], phases, commentEnabled = false, finished = false }: {
  activities?: SkillActivity[]; phases: Record<string, string>; commentEnabled?: boolean; finished?: boolean
}) {
  const { catalog, error } = useAgentCatalog()
  const [filter, setFilter] = useState('')
  const agents = [...(catalog?.agents ?? [])].sort((a, b) => roleOrder.indexOf(a.role) - roleOrder.indexOf(b.role))
  const filtered = useMemo(() => activities.filter(item => !filter || item.agent === filter), [activities, filter])
  const groups = useMemo(() => groupSkillRecords(filtered), [filtered])
  const stats = useMemo(() => skillRecordStats(filtered), [filtered])
  const scopeLabel = filter ? agents.find(agent => agent.id === filter)?.label ?? filter : '全部 Agent'
  return <section className="agent-skill-section" aria-label="Agent 技能与执行记录">
    <div className="skill-section-heading"><div><span className="section-kicker">03 / 专业协作</span><h2>八类 Agent 与专项技能</h2></div><p className="muted">配置说明能力，执行记录说明本次实际使用。</p></div>
    {error && <p role="alert">{error}；无法展示完整角色目录，实际执行记录仍可查看。</p>}
    {catalog?.errors.map(item => <p className="error" key={item}>{item}</p>)}
    {!catalog && !error && <p className="muted">正在读取角色与技能…</p>}
    <div className="agent-skill-grid">{agents.map(agent => {
      const latest = activities.filter(item => item.agent === agent.id).at(-1)
      const disabled = !agent.enabled || (agent.role === 'comment_analyst' && !commentEnabled && !latest)
      return <article key={agent.id} className={`agent-skill-card${latest?.status === 'running' ? ' skill-is-running' : ''}`}>
        <header><h3>{agent.label}</h3><span>{disabled ? '未启用' : latest ? activityStatus(latest, finished) : phases[agent.id] ? '已参与，未记录技能活动' : finished ? '本次未参与' : '等待工作单'}</span></header>
        <p>{agent.description}</p><AgentSkillSummary agent={agent} />
        {latest && <div className="current-skill"><strong>最近使用：{latest.label}</strong><p>{stepLabel(latest)} · {modeLabel(latest)}</p></div>}
      </article>
    })}</div>
    <details className="skill-activity-log" open={activities.length > 0}>
      <summary>技能使用记录 · 全任务 {activities.length} 条处理记录</summary>
      <label className="skill-filter">查看角色<select value={filter} onChange={event => setFilter(event.target.value)}><option value="">全部 Agent</option>{agents.map(agent => <option key={agent.id} value={agent.id}>{agent.label}</option>)}</select></label>
      <p className="skill-scope-summary" role="status">当前范围：{scopeLabel} · {stats.records} 条处理记录 · {stats.skills} 项专项技能 · {stats.modelSelections} 条模型选用记录</p>
      <details className="skill-count-help"><summary>这些数字如何计算？</summary>
        <p>每条记录表示一个使用技能说明的处理请求。同一技能可用于生成、审查、修订等多个步骤；同一记录的状态更新不重复计数。</p>
        <p>专项技能按技能标识去重，共享基础技能不另加记录；模型选用仅统计明确标记为模型选用的记录。程序匹配、沿用与回退分别注明，不算模型选用。</p>
        <p>这里不统计模型接口实际请求次数，技能选择请求和底层重试由模型运行统计另行记录。加载本地技能说明本身不请求模型，说明随处理请求发送会增加输入 token。</p>
      </details>
      <p className="muted">记录按角色与技能版本归组。所属阶段保存产物，不表示每条响应被采纳；事实是否核验通过以证据台账和报告为准。</p>
      {!filtered.length && <p className="muted">{filter ? '当前角色没有技能使用记录。' : '尚无技能使用记录。'}旧任务不会补造使用历史。</p>}
      {[...groups].reverse().map(group => <SkillRecordGroup key={group.key} group={group} agentLabel={agents.find(agent => agent.id === group.records[0].agent)?.label ?? group.records[0].agent} finished={finished} expanded={Boolean(filter) || groups.length === 1} />)}
    </details>
  </section>
}
