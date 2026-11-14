import type { AgentCatalog } from '../api/client'

export const agentCatalogFixture: AgentCatalog = {
  errors: [], read_only: true,
  agents: [
    ['fact_investigator', 'analyst_a', '事实调查 Agent', '事件事实链重建'],
    ['media_propagation', 'analyst_b', '媒体传播 Agent', '报道口径追踪'],
    ['history_insight', 'analyst_c', '历史洞察 Agent', '可比案例与机制对照'],
    ['comment_insight', 'comment_analyst', '评论洞察 Agent', '评论样本问题与诉求归纳'],
    ['moderator', 'moderator', '协作主持 Agent', '缺口诊断与定向协作'],
    ['evidence_verifier', 'verifier', '证据核验 Agent', '陈述与证据对照核验'],
    ['reporter', 'reporter', '报告综合 Agent', '证据约束的专报综合'],
    ['task_preparation', 'utility', '任务准备 Agent', '调查范围识别与准备'],
  ].map(([id, role, label, skill]) => ({
    id, role, label, main_skill: `${id}-method`, enabled: true, selection_mode: 'work_order',
    description: `${label} 的职责说明`,
    skills: [{ id: `${id}-method`, label: skill, description: '用于指定工作单', version: '1.0.0', digest: 'abc123', body: '输入：实际材料。\n步骤：核对来源。\n停止：材料不足保留缺口。', output: '有来源的阶段产物', shared: false }],
  })),
}
