import type { SkillActivity } from '../events/state'

const stages: Record<string, string> = {
  plan: '制定调查计划', summarize: '整理证据与陈述', reflect: '检查调查缺口',
  report_draft: '生成报告草稿', report_review: '审查与修订报告分析',
  report_summary: '生成或修订报告摘要', report_summary_review: '审查报告摘要',
  summary_entailment: '核对摘要与事实', verify: '核对陈述与证据', verification: '核对陈述与证据',
  moderation: '评审调查与分派任务', public_interest: '检查任务公共性',
  scope_review: '审查调查范围与隐私', source_scope_classification: '识别信源地域',
  translation: '转译材料', search_query_translation: '准备跨语言检索词',
  cross_language_subject_relevance: '核对跨语言主体', topic_discovery: '准备事件检索',
  comment_classification: '整理评论样本', comment_theme_review: '审查评论主题',
  comment_synthesis: '归纳评论问题与诉求',
}
const roleSteps: Record<string, string> = {
  reporter: '报告内容处理', verifier: '证据与内容审查', utility: '任务与材料准备',
  moderator: '调查协作评审', comment_analyst: '评论样本处理',
  analyst_a: '事实调查处理', analyst_b: '传播材料处理', analyst_c: '历史案例处理',
}
const statuses: Record<string, string> = {
  loaded: '技能说明已准备', running: '请求处理中', response_ready: '响应已返回，采纳未确认',
  stage_saved: '所属阶段已保存产物', stage_partial: '所属阶段部分完成',
  failed: '请求未完成', cancelled: '请求已取消',
}
const modes: Record<string, string> = {
  model: '模型选用', work_order: '程序按工作阶段匹配',
  fallback: '程序回退', continued: '沿用前序技能',
}
const genericOutcomes = new Set([
  '所属处理阶段已结束，报告产物已保存；候选是否采纳以报告与证据台账为准。',
  '模型响应已返回；是否采纳由后续业务校验与入库决定。',
  '模型响应已返回；本条响应是否采纳仍由业务校验与入库决定。',
])

export function stepLabel(item: SkillActivity): string {
  if (item.stage === 'report_review' && item.role === 'reporter') return '修订报告分析'
  if (item.stage === 'report_review' && item.role === 'verifier') return '审查报告分析'
  return stages[item.stage] ?? roleSteps[item.role] ?? '其他处理（具体阶段未记录）'
}

export function modeLabel(item: SkillActivity): string {
  return modes[item.selection_mode] ?? '选用方式未记录'
}

export function activityStatus(item: SkillActivity, finished: boolean): string {
  if (finished && ['selected', 'loaded', 'running'].includes(item.status)) return '任务已结束，处理结果未记录'
  if (item.status === 'selected') {
    if (item.selection_mode === 'model') return '模型已选用技能'
    return modes[item.selection_mode] ? '程序已确定技能' : '已确定技能，选用方式未记录'
  }
  return statuses[item.status] ?? '处理状态未识别'
}

export function selectionExplanation(item: SkillActivity): { label: string; text: string } {
  if (item.selection_mode === 'model') return { label: '模型选用理由', text: item.reason || '未记录具体理由。' }
  if (item.selection_mode === 'work_order') return {
    label: '程序匹配说明',
    text: `程序根据“${stepLabel(item)}”工作阶段加载此技能；本条没有进行模型选用。`,
  }
  if (item.selection_mode === 'continued') return { label: '沿用说明', text: '本次处理沿用前序已确定的技能，没有再次进行模型选用。' }
  if (item.selection_mode === 'fallback') return { label: '回退说明', text: item.reason || '模型选用未得到合格结果，程序使用本角色的基础技能。' }
  return { label: '选用方式', text: '这条历史记录未注明选用方式，无法确认由模型选用还是程序匹配。' }
}

export function outcomeExplanation(item: SkillActivity, finished: boolean): string {
  if (finished && ['selected', 'loaded', 'running'].includes(item.status)) return '任务已结束，但这条记录没有处理结束信息，无法确认请求是否完成。'
  if (item.outcome && !genericOutcomes.has(item.outcome)) return item.outcome
  if (item.status === 'stage_saved') return '所属阶段的产物已保存。阶段引用不表示本条响应被采纳，也不代表事实已核验。'
  if (item.status === 'stage_partial') return '所属阶段仅部分完成，仍可能存在缺口；阶段产物不表示本条响应被采纳。'
  if (item.status === 'response_ready') return finished
    ? '模型响应已返回，任务已结束；这条记录没有后续采纳结果。'
    : '模型响应已返回，是否采纳仍需业务校验与入库确认。'
  return '尚未记录处理结果。'
}

export type SkillUsageGroup = { key: string; records: SkillActivity[] }

export function groupSkillRecords(records: SkillActivity[]): SkillUsageGroup[] {
  const groups = new Map<string, SkillUsageGroup>()
  for (const item of records) {
    const identity = [item.agent, item.skill_id, item.version, item.digest]
    if (!item.version || !item.digest) identity.push(item.invocation_id)
    const key = JSON.stringify(identity)
    const group = groups.get(key) ?? { key, records: [] }
    group.records.push(item)
    groups.set(key, group)
  }
  return [...groups.values()]
}

export function skillRecordStats(records: SkillActivity[]) {
  return {
    records: records.length,
    skills: new Set(records.map(item => item.skill_id)).size,
    modelSelections: records.filter(item => item.selection_mode === 'model').length,
  }
}

export function groupNotice(records: SkillActivity[], finished: boolean): string {
  const notices = []
  if (records.some(item => item.status === 'failed')) notices.push('含未完成请求')
  if (records.some(item => item.status === 'cancelled')) notices.push('含已取消请求')
  if (records.some(item => item.status === 'stage_partial')) notices.push('含阶段部分完成记录')
  if (records.some(item => ['selected', 'loaded', 'running'].includes(item.status))) notices.push(finished ? '含结果未记录的处理' : '仍有处理尚未返回')
  return notices.join('；')
}
