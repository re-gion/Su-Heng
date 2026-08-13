# 报告 IR 参考实例与规则验证脚本（v0.1）

这几份文件是 [`../05-核心契约.md`](../05-核心契约.md) 的可执行版本。文档里的 JSONC 片段带注释、不完整，只用来讲解；**要照着敲代码、要写测试，用这里的文件。**

| 文件 | 是什么 |
|---|---|
| `report-ir-v0.1.agent-output.json` | **报告 Agent 的原始输出**。只有排版与叙述，没有一个权威字段 |
| `report-ir-v0.1.db-seed.json` | 对应的数据库内容（`claim` / `evidence` / `claim_evidence` 三张表的行 + 每条 claim 的期望徽章与命中规则） |
| `report-ir-v0.1.fixture.json` | **渲染层按 ref 从数据库回填之后**的完整 IR，能通过 §2.4 的 R1–R18 |
| `validate_fixture.py` | 把上面三份对起来跑一遍静态校验 |
| `check_badge_decision_table.py` | 穷举验证 §4.3 的 D1–D11 决策表互斥、穷举、且不会"有反证却判绿" |
| `check_ddl_invariants.py` | 抽取 `02-系统架构设计.md` §6 的建表 SQL 在 SQLite 跑一遍，验证非法状态确实写不进去 |

对照前两份和第三份看，就能一眼看出 §0.5「权威字段单一事实源」到底是什么意思：模型交上来的核查表条目只有 `{"claim_ref": "C001"}`，正文、徽章、结论、引用卡片全是渲染层填的。

## 跑一下

```bash
python docs/方案包/fixtures/validate_fixture.py            # 34/34 通过
python docs/方案包/fixtures/check_badge_decision_table.py  # 100 组，非唯一命中 0，误判绿 0
python docs/方案包/fixtures/check_ddl_invariants.py        # 11 张表建成，8 种非法写入全部被拒
```

（Windows 终端中文乱码时加 `PYTHONIOENCODING=utf-8`。三个脚本只用标准库，无需装依赖。）

`validate_fixture.py` 检查的是能纯静态判定的部分：

- 顶层结构与版本字段（`schema_version` / `min_reader_minor`）、`block_id` 唯一
- **R2** 无悬空 `claim_ref` / `evidence_ref`
- **R17** 引用闭包：带 `claim_ref` 的条目，其 `evidence_refs` / `quote_ref` 必须是该 claim 绑定证据的子集
- **R3** 核查表引用卡片非空，且与 `claim_evidence` 的绑定集合完全相等
- **R15** 回填后的权威字段与数据库逐字段相等；引用卡片的 `relation`/`quote`/偏移与 `claim_evidence` 一致
- `verbatim` 引用满足 §0.3 写入期不变量（偏移非空 + `quote_verified=1`）
- **R16** 未完成核验的 claim 不允许带 verified/refuted/disputed
- **R4–R8**、**R11–R13**（illustrative 拦截、编辑判断依据、01/03 禁编辑判断、数值块 data_basis、时间线与历史卡证据非空、局限性必出、`limitation_ref` 成对、证据附录闭包）
- **R18** 指标恒等式 `candidate = rendered + rejected`，以及全部比率、计数与种子数据实算一致（含按 `publisher_entity` 归并后的独立信源数）
- §0.5：Agent 原始输出里不含任何权威字段，也不产出 `metrics`/`task`/证据附录/局限性/报告头

**没检查的**（需要运行时才能做，属于 M0 的实现任务）：R1 的跨版本兼容矩阵、R9 的 `verbatim` 原文子串匹配（要有 `content_text`）、R14 的蕴含校验（要调 `verifier`）、R10 的 fact+refuted 守卫（本实例里不存在这种数据）。

## 这份实例故意埋的几个点

| 点 | 位置 | 为什么埋 |
|---|---|---|
| 一稿多站归并 | `E003`（每日经济新闻）与 `E007`（新浪财经转载）的 `publisher_entity` 都是"每日经济新闻" | 10 条证据只算 **9 个独立主体**——验证独立信源数不是按域名数 |
| 当事方不能自证 | `C004` 绑 `E004`（企业官网，L1，`party`），关系 `partial` | 徽章落 `unverified`（D11）而不是"单源·官方"，验证 L1 ≠ 裁判资格 |
| 有反证就不判绿 | `C006` 同时有 `support`（E008）和 `contradict`（E009） | 落 `disputed`（D2），验证支持方存在也不能盖过反证 |
| 全 snippet 也能进正文 | `C001` 的 `E003` 是 `discovered`，卡片带"原文未取得" | 验证用户拍板的宽松口径 |
| 被拦截的条目要计数 | `C008` 无绑定证据，只在种子里、不在 IR 里 | 验证 `candidate(8) = rendered(7) + rejected(1)`，"引用覆盖率 100%"不是过滤后的恒等式 |
| 抓取失败仍可引用 | `E009` 是 `fetch_failed`，卡片带"原文抓取失败" | 验证三态抓取状态机 |

## 改的时候

三份文件是一体的，改一份就要改另外两份，然后重跑脚本。M0-9 起把它接进 `backend/tests/fixtures/`，作为渲染器的第一条回归数据（路线图 §2.3）。字段增删按 05 §6 的登记规则升版本号。
