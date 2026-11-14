import { useEffect, useState } from 'react'
import { getAgentCatalog, type AgentCapability, type AgentCatalog } from '../api/client'
import type { SkillActivity } from '../events/state'
import './agent-skills.css'

const roleOrder = ['utility', 'analyst_a', 'analyst_b', 'analyst_c', 'comment_analyst', 'moderator', 'verifier', 'reporter']
const statusLabels: Record<string, string> = {
  selected: '已选择', loaded: '方法已加载', running: '调用中', response_ready: '响应已返回，待业务处理',
  stage_saved: '阶段产物已保存', stage_partial: '阶段部分完成', failed: '调用未完成', cancelled: '已取消',
}
const modeLabels: Record<string, string> = { model: 'Agent 选择', work_order: '按工作单加载', fallback: '程序回退', continued: '沿用本轮方法' }

function activityStatus(item: SkillActivity, finished: boolean) {
  if (finished && ['selected', 'loaded', 'running'].includes(item.status)) return '任务已结束，未记录调用完成'
  return statusLabels[item.status] ?? item.status
}

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
      <p className="muted">{agent.selection_mode === 'model' ? '规划与反思时在允许的方法中选择，处理材料时沿用本轮方法。' : '根据工作单类型加载对应方法，保持原有校验和权限约束。'}技能内容只读。</p>
      {agent.skills.map(skill => <section key={skill.id} className="skill-method">
        <h4>{skill.label}<small>{skill.shared ? '共享' : '专项'} · v{skill.version}</small></h4>
        <p>{skill.description}</p><p><strong>预期产物：</strong>{skill.output}</p>
        <pre>{skill.body}</pre><small className="skill-version">{skill.id} · {skill.digest}</small>
      </section>)}
    </details>
  </div>
}

export function AgentSkillRoster({ activities = [], phases, commentEnabled = false, finished = false }: {
  activities?: SkillActivity[]; phases: Record<string, string>; commentEnabled?: boolean; finished?: boolean
}) {
  const { catalog, error } = useAgentCatalog()
  const [limit, setLimit] = useState(12)
  const [filter, setFilter] = useState('')
  const agents = [...(catalog?.agents ?? [])].sort((a, b) => roleOrder.indexOf(a.role) - roleOrder.indexOf(b.role))
  const filtered = activities.filter(item => !filter || item.agent === filter)
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
        {latest && <div className="current-skill"><strong>最近使用：{latest.label}</strong><p>{latest.reason}</p></div>}
      </article>
    })}</div>
    <details className="skill-activity-log" open={activities.length > 0}>
      <summary>本次技能执行记录 · {activities.length} 次方法调用</summary>
      <p className="muted">响应返回不代表候选内容被采纳；阶段引用来自已保存的实际产物，核验结论以证据台账和报告为准。</p>
      <label className="skill-filter">查看角色<select value={filter} onChange={event => { setFilter(event.target.value); setLimit(12) }}><option value="">全部 Agent</option>{agents.map(agent => <option key={agent.id} value={agent.id}>{agent.label}</option>)}</select></label>
      {!filtered.length && <p className="muted">尚无技能调用记录。旧任务不会补造使用历史。</p>}
      {filtered.slice(-limit).reverse().map(item => <details key={item.invocation_id} className="skill-activity">
        <summary><strong>{item.label}</strong><span>{agents.find(agent => agent.id === item.agent)?.label ?? item.agent} · {activityStatus(item, finished)}</span></summary>
        <p><strong>任务依据：</strong>{item.reason}</p><p>{modeLabels[item.selection_mode] ?? item.selection_mode} · 阶段 {item.stage} · v{item.version}</p>
        <p>{item.outcome || '方法已登记，尚无阶段产物。'}</p>
        {item.claim_refs.length > 0 && <p>阶段陈述：{item.claim_refs.join('、')}</p>}
        {item.evidence_refs.length > 0 && <p>阶段证据：{item.evidence_refs.map(ref => <a key={ref} href={`#evidence-${ref}`}>{ref} </a>)}</p>}
        {item.report_id && <p><a href={`/api/reports/${encodeURIComponent(item.report_id)}/html?view=full`} target="_blank" rel="noreferrer">查看阶段报告</a></p>}
        <small className="skill-version">方法 {item.skill_id} · 内容指纹 {item.digest} · 调用 {item.invocation_id}</small>
      </details>)}
      {filtered.length > limit && <button type="button" onClick={() => setLimit(value => value + 24)}>查看更早记录</button>}
    </details>
  </section>
}
