# 从采集到成文的质量缺陷根因
Type: research
Status: resolved
Assignee: pipeline-audit
Parent: ../map.md
Blocked by:

## Question
检查当前源码及已有改动，定位影响分析深度、真实指标、重复和模板拼接的最早根因，提出有代码位置和回归方法的修复优先级。

## Answer

审计见 [`../research/pipeline-audit.md`](../research/pipeline-audit.md)。最早根因在调查 Agent 的总结输入：全量证据被压成标题与 snippet 后做单次尾截断，原文和来源/时间/抓取状态等结构化字段在成文前即丢失；多轮只做 claim 文本逐字去重。forum 再把 claim 用分号拼成 summary，完整报告将同一 summary 跨观点与历史章节复用。执行摘要只是前 12 条 claim，报告 Agent 只能补一句组织建议，附录则确定性展开全量证据及长节选。

当前真实可计算的是任务内证据、引用、抓取、信源结构、发布日期覆盖、核验关系，以及已真实采集的热榜/确认评论样本；普通 web search 不能推出全网声量、曝光、代表性情感比例、传播峰值或走势。修复顺序应先改 Agent 的有界均衡证据输入和已有 claim 新增性约束，再建立结构化分析/决策建议契约与附录分层，最后按真实数据可用性选择图表。
