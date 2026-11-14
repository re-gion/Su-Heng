import { describe, expect, it } from 'vitest'
import type { SkillActivity } from '../events/state'
import { activityStatus, groupNotice, groupSkillRecords, outcomeExplanation, selectionExplanation, skillRecordStats, stepLabel } from './skillPresentation'

function record(changes: Partial<SkillActivity> = {}): SkillActivity {
  return {
    invocation_id: 'one', agent: 'reporter', role: 'reporter', skill_id: 'report', label: '专报综合',
    version: '1.0.0', digest: 'file-one', stage: 'report_summary', status: 'response_ready',
    reason: '', selection_mode: 'work_order', outcome: '', claim_refs: [], evidence_refs: [], report_id: '', at: '', ...changes,
  }
}

describe('skill usage meanings', () => {
  it('keeps different roles, versions and file contents separate without counting them as new skill types', () => {
    const records = [record(), record({ invocation_id: 'two', stage: 'report_draft' }),
      record({ invocation_id: 'three', version: '1.0.1' }), record({ invocation_id: 'four', digest: 'changed-file' }),
      record({ invocation_id: 'five', agent: 'another-agent' })]
    const groups = groupSkillRecords(records)
    expect(groups).toHaveLength(4)
    expect(groups[0].records).toHaveLength(2)
    expect(skillRecordStats(records)).toEqual({ records: 5, skills: 1, modelSelections: 0 })
    expect(records).toHaveLength(5)
    expect(groupSkillRecords([record({ digest: '' }), record({ invocation_id: 'unknown-two', digest: '' })])).toHaveLength(2)
  })

  it('counts only explicit model selection and labels continuation, fallback and unknown history honestly', () => {
    const records = ['model', 'continued', 'fallback', 'work_order', ''].map(selection_mode => record({ selection_mode }))
    expect(skillRecordStats(records).modelSelections).toBe(1)
    expect(selectionExplanation(record({ selection_mode: 'model', reason: '核对日期冲突。' }))).toEqual({ label: '模型选用理由', text: '核对日期冲突。' })
    expect(selectionExplanation(records[1]).text).toContain('没有再次进行模型选用')
    expect(selectionExplanation(records[2]).label).toBe('回退说明')
    expect(selectionExplanation(records[4]).text).toContain('无法确认')
  })

  it('does not infer acceptance from a saved stage or leave a finished task waiting for a response', () => {
    const saved = record({ status: 'stage_saved', outcome: '所属处理阶段已结束，报告产物已保存；候选是否采纳以报告与证据台账为准。' })
    expect(outcomeExplanation(saved, true)).toContain('不表示本条响应被采纳')
    expect(outcomeExplanation(saved, true)).toContain('不代表事实已核验')
    const interrupted = record({ status: 'running' })
    expect(activityStatus(interrupted, true)).toContain('处理结果未记录')
    expect(outcomeExplanation(interrupted, true)).toContain('无法确认请求是否完成')
    expect(outcomeExplanation(record(), true)).toContain('没有后续采纳结果')
    expect(groupNotice([record({ status: 'failed' }), interrupted], true)).toContain('结果未记录')
  })

  it('uses readable step names without inventing a precise stage for unknown codes', () => {
    expect(stepLabel(record())).toBe('生成或修订报告摘要')
    expect(stepLabel(record({ stage: 'report_review' }))).toBe('修订报告分析')
    expect(stepLabel(record({ stage: 'report_review', role: 'verifier' }))).toBe('审查报告分析')
    expect(stepLabel(record({ stage: 'future-stage' }))).toBe('报告内容处理')
    expect(stepLabel(record({ role: '', stage: '' }))).toBe('其他处理（具体阶段未记录）')
    expect(activityStatus(record({ status: 'selected' }), false)).toBe('程序已确定技能')
    expect(activityStatus(record({ status: 'selected', selection_mode: 'model' }), false)).toBe('模型已选用技能')
    expect(activityStatus(record({ status: 'selected', selection_mode: '' }), false)).toBe('已确定技能，选用方式未记录')
  })
})
