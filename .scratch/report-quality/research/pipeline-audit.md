# 报告质量生成链路审计

## 结论

当前报告的信息密度问题，最早发生在调查 Agent 的输入和产物契约，而不是 HTML 样式。调查 Agent 在总结阶段只看到按数据库顺序拼接的 `证据编号 | 标题 | 搜索摘要`，整包再从尾部截到 24,000 字符；它看不到原文、发布日期、信源角色、来源主体、抓取状态，也不知道已有陈述。后续编排只能保存短 claim，再把这些 claim 机械拼成论坛摘要、观点、历史卡片和执行摘要，因此重复、低密度和“模板拼接感”是稳定结果。

工作区当前未提交改动已经新增证据漏斗、核验矩阵、纠偏时间线和数据质量说明，这些能改善“指标是否真实”和读者对证据质量的理解，但仍未改变上述上游根因。

## 1. 链路与最早失真点

### 1.1 检索层拥有比 Agent 实际看到的更多数据

搜索结果契约本身包含 `url/title/snippet/summary/published_at/source_name/provider/lang`，见 [`backend/yuqing/core/search/base.py`](../../../backend/yuqing/core/search/base.py#L21-L30)。入库时还补充了 `publisher_entity/source_role/source_tier/retrieval_query/fetch_status/content_text`，见 [`backend/yuqing/services/evidence_store.py`](../../../backend/yuqing/services/evidence_store.py#L27-L52) 与 [`backend/yuqing/storage/schema.sql`](../../../backend/yuqing/storage/schema.sql#L26-L53)。

因此“数据源完全没有结构化字段”不是根因。最早的丢失在 [`backend/yuqing/agents/openai_runtime.py`](../../../backend/yuqing/agents/openai_runtime.py#L52-L68)：

- 所有证据只投影成 `local_id | title | snippet`；抓到的 `content_text` 完全不用。
- 不传 `published_at/source_role/source_tier/publisher_entity/fetch_status/lang/retrieval_query`。
- 按当前顺序拼接后直接 `[:24000]`，证据多时稳定截掉后半部分；多个 Agent 并行检索后，较晚入库的纠偏材料和外文材料更容易消失。
- 同一全量 inventory 同时交给事实、传播和历史 Agent，只有 system prompt 不同，没有角色化证据选择与均衡配额。
- 输出只有 `text/statement_kind/evidence_ids`，不能表达“数字及口径”“回应时点”“传播主体变化”“相似案例维度”“决策含义”等分析中间产物。

这解释了为什么提高模型 token 或美化 renderer 不会从根本上改善报告：模型在成文前已经失去原文和关键元数据。

### 1.2 多轮和多 Agent 去重只做逐字等值

[`backend/yuqing/services/v1_orchestrator.py`](../../../backend/yuqing/services/v1_orchestrator.py#L354-L358) 在进入循环前仅保存已有 claim 文本集合；每轮仍把任务中的全量 evidence 交给 `summarize`（同文件 L430-L436），写入前只判断 `item.text in claims_before`（L439-L476）。

因此：

- “学校发布情况说明”与“校方作出公开回应”会同时保留；
- 外层新一轮即使没有新证据，也可能换一种措辞再产出相同事实；
- 不能把“同一主题但新增数字、时间、反证或口径”的陈述与纯近义重复区分开；
- Agent 不知道已有陈述，只能依靠写入后的逐字过滤，浪费总结调用与 claim 预算。

去重应先让模型明确看到有界的已有陈述，并要求只有出现新证据、新数字/日期、新矛盾或新决策含义时才新增；确定性写入层仍保留精确去重，不能用模糊相似度自动删除可能含关键差异的 claim。

### 1.3 编排把 claim 串接成摘要，随后跨章节复用

调查结束时，[`backend/yuqing/services/v1_orchestrator.py`](../../../backend/yuqing/services/v1_orchestrator.py#L520-L550) 把最近八条 finding 用分号连接为一个 forum `summary`。完整报告又：

- 把每个 Agent 的整段 summary 直接当“情感与观点”条目（[`backend/yuqing/services/full_report.py`](../../../backend/yuqing/services/full_report.py#L386-L404)）；
- 把 `history_insight` 的同一段 summary 再当“历史对照”卡片（同文件 L449-L480）。

同一段文字同时进入观点和历史章节，是用户看到“历史对照重复”的直接原因。更早原因仍是 forum summary 没有分类型的结构，报告层无法安全拆分。

### 1.4 报告 Agent 没有被授权生成真正的综合分析

执行摘要在 [`backend/yuqing/services/report_builder.py`](../../../backend/yuqing/services/report_builder.py#L196-L251) 中直接取前 12 条 claim 原文，`why` 和 `so_what` 固定为空。综合报告 Agent 的实现只允许返回一句 `organization_note` 和 `section_warnings`，输入也只有 task、metrics 与 forum，见 [`backend/yuqing/agents/reporter.py`](../../../backend/yuqing/agents/reporter.py#L13-L20)。

完整报告的建议主体还是固定句“优先回看证据卡……”，并任取前三条 evidence 作为依据；reporter 的一句建议也挂到相同三条材料，见 [`backend/yuqing/services/full_report.py`](../../../backend/yuqing/services/full_report.py#L485-L521)。这既无法形成面向高校/机构决策者的“事实—风险—行动—触发条件”，也可能让建议与所挂证据缺少语义对应。

### 1.5 附录占比大是确定性行为，不是偶发排版

简报构建器先把所有 claim 绑定材料放入附录（[`backend/yuqing/services/report_builder.py`](../../../backend/yuqing/services/report_builder.py#L77-L105)）；完整报告再补入任务中所有未被引用 evidence，并为每条保留最多 1,200 字原文/摘要（[`backend/yuqing/services/full_report.py`](../../../backend/yuqing/services/full_report.py#L53-L103)）。renderer 对每条输出元数据、全部 claim 引文、原文节选和翻译（[`backend/yuqing/render/html.py`](../../../backend/yuqing/render/html.py#L129-L169)），并把附录强制标为速览可见（同文件 L427-L449）。

所以证据附录占报告大部分是当前产品规则必然导致的。合理修复是正文只展示实际引用证据的紧凑卡片，未引用库存进入折叠的数据质量/检索日志；完整证据仍保留在证据包或按需展开，不能为缩短页面而破坏可追溯性。

## 2. 当前可用数据、可计算指标与边界

### 2.1 可以真实计算

| 指标/分析 | 数据来源 | 合法口径 |
| --- | --- | --- |
| 证据总量、实际引用量、原文取得率、抓取失败量 | `evidence`、`claim_evidence` | 本任务检索库存和关键陈述引用情况 |
| 发布日期分布、首次/末次材料日期 | `evidence.published_at` | 已检索材料的日期覆盖，不是全网声量曲线 |
| 信源角色、等级、来源主体分布 | `source_role/source_tier/publisher_entity` | 已检索材料结构；未知/转载/当事方须单列 |
| claim 的支持、部分支持、反证、未提及矩阵 | `claim_evidence.relation` | 逐陈述证据关系，不是民意分布 |
| 已证实/争议/证伪/待核验比例 | `claim.badge` 与实际渲染条目 | 报告陈述核验状态，不是系统成功率 |
| 本地热榜排名与热度时间序列 | `hot_snapshot` | 仅当真实采集点存在且 `heat_value` 可比较；表结构见 [`schema.sql`](../../../backend/yuqing/storage/schema.sql#L194-L208) |
| 用户确认帖子的评论数、点赞/回复与样本主题 | `comment_collection/social_comment` | 只代表用户确认帖子的已采集样本；表结构见 [`schema.sql`](../../../backend/yuqing/storage/schema.sql#L59-L109) |
| 有来源的历史案例及结构化对照维度 | `historical_event/task_history_match` | 仅限已导入、有许可和来源的历史库；匹配逻辑见 [`historical_data.py`](../../../backend/yuqing/services/historical_data.py#L285-L344) |

当前工作区新增的证据漏斗和核验矩阵（[`full_report.py`](../../../backend/yuqing/services/full_report.py#L195-L293)）符合上述口径；纠偏时间线只用实际引用且有日期的材料（L143-L193），也比把所有检索结果画成“传播趋势”更可靠。

### 2.2 现有普通检索链无法得到

以下不能从 `web_search top_k` 或证据条数推导：

- 全网发文量、曝光量、阅读/播放量、覆盖人数；
- 平台总体互动量或跨平台可比的“热度”；
- 总体正负面情感比例、公众支持率；
- 真实传播路径、首发到转载的完整网络、峰值和拐点；
- 机构声誉损失金额、概率预测、未来走势。

搜索结果契约只有内容与来源元数据，没有曝光/互动/总体样本基数，见 [`backend/yuqing/core/search/base.py`](../../../backend/yuqing/core/search/base.py#L11-L30)。搜索命中数还受 top_k、查询词、provider 去重和排序影响，最多只能称为“本任务检索到的材料数”。

要输出真实声量/互动趋势，必须接入具有时间序列和明确口径的平台数据或持续采集任务；当前唯一接近的来源是 `hot_snapshot`，且只有命中目标事件、数值存在、同平台同口径时才能画曲线。评论字段虽有点赞/回复数，也不能外推平台总体民意。

## 3. 当前未提交改动的作用与剩余缺口

当前 `full_report.py` 已加入：

- 证据获取漏斗；
- 关键陈述—信源核验矩阵；
- 基于实际引用材料的纠偏时间线；
- 引用/抓取/信源丢弃原因和数据质量分布；
- 只在真实 `hot_snapshot` 有数值时输出热榜曲线。

这些改动解决了“把证据数冒充声量”和“没有真实指标”的一部分展示问题，但尚未解决：

1. Agent 输入仍只含 snippet 且尾截断；
2. 多轮近义重复；
3. forum summary 跨观点/历史复用；
4. 执行摘要没有 why/so_what；
5. 建议固定且证据绑定任意；
6. 附录全部展开。

因此当前结果应视为“数据透明度改善”，不能称为完整的内容质量优化。

## 4. 修复优先级

### P0：修复调查 Agent 的证据输入和新增性约束

1. 构造有界 evidence digest：每条包含编号、标题、来源主体/角色/等级、发布日期、抓取状态、语言、摘要与原文关键节选。
2. 不再对整包尾截断；按证据数分配字符预算，至少保证每条都有元数据和摘要，原文按均衡预算截取。需要设总条数上限时，优先覆盖不同来源主体、不同日期、不同抓取状态，并明确未纳入数量。
3. 角色化 plan：事实 Agent 查事件拆分、权威通报与回应时间；传播 Agent 查首发/回应/转载关系与可回溯数字口径；历史 Agent 查可比事件及结局维度。
4. 在 scoped query 中加入有界已有 claim，要求新增 claim 必须带来新证据、新数字/日期、新矛盾或新分析维度。保留精确去重；不要用字符相似度自动丢弃含数字、否定或主体差异的陈述。

### P1：建立结构化分析产物和决策者报告契约

1. forum 不再存一段分号串；至少区分 `key_facts/response_timeline/propagation_observations/historical_comparisons/risks/actions/gaps`。
2. reporter 输入实际 claim、引用关系和证据元数据，在不改写权威字段的前提下组织 what/why/so_what、决策建议及触发条件。
3. 建议逐条绑定支撑 claim/evidence；没有依据时明确标为编辑判断，并说明推导链，不能任取前三条 evidence。
4. 历史章节只消费历史结构，不再复用 Agent 总结段；观点章节只消费有明确主体与来源的观点事实。

### P1：附录分层

1. 正文显示“实际引用材料”紧凑卡片；原文节选默认折叠。
2. 未被正文引用的检索库存放入折叠清单或单独证据包。
3. 速览不强制展开 evidence appendix；保持 claim 到 evidence 的锚点和离线证据包能力。

### P2：指标与图表选择器

按数据可用性确定输出：

- 有 ≥2 个可靠日期：纠偏/回应时间线；
- 有多主体核验：claim—source 矩阵；
- 有真实 hot snapshot 数值：同口径热榜曲线；
- 有用户确认评论样本：样本量、平台、采样方法及有限主题；
- 其余情况用表格或文字边界，不为了“有图”画证据数量柱状图。

## 5. 真实回归建议

### 单元与集成测试

1. **后半材料不丢失**：构造超过 24,000 字且关键反证位于最后的 evidence inventory，断言 prompt 中仍出现该证据的 ID、来源元数据和关键节选。
2. **均衡预算**：短摘要、长原文、不同语言和不同 source_role 混合时，每条证据都进入 digest，单条长原文不能挤掉其他证据。
3. **角色计划**：三个 Agent 的 plan prompt 分别包含回应时点/相关事件拆分、传播主体/可回溯数字口径、历史事件/结局维度，且明确禁止全网估计和主观情感比例。
4. **已有 claim 可见**：第二轮 summarize prompt 包含已有陈述清单及新增性规则；测试新增数字、日期或反证仍允许输出，纯改写由模型指令抑制、精确重复由编排器拒绝。
5. **跨章节唯一性**：同一 forum message 不得同时生成 viewpoint 与 history card；历史卡必须有独立事件名、来源和对照维度。
6. **建议闭包**：每条事实性建议的 evidence_refs 必须与其 claim 绑定证据相交；编辑建议必须有明确 editorial basis。
7. **附录预算**：速览不渲染未引用 evidence；完整正文的展开节选总长度受限，证据包仍包含全量记录。
8. **指标禁区**：没有 hot snapshot/comment collection 时，IR 不得出现“全网声量、热度峰值、正负面比例、公众支持率”。

### 样本回放

用两份武汉大学报告对应任务数据做固定回放，至少记录：

- 正文（不含附录）字数与附录字数比例；
- 规范化后重复句/重复 claim 数；
- 执行摘要 what/why/so_what 是否非空且各自引用闭包成立；
- 历史卡数量、唯一事件数、每卡证据数；
- 图表数及每张图的真实数据表/口径；
- 建议中有明确主体、动作、时限/触发条件、证据依据的比例。

自动化通过只证明固定数据下的结构和不变量成立。最终仍需用一次真实在线调查确认搜索覆盖、外部服务、网页抓取和实际报告阅读质量。
