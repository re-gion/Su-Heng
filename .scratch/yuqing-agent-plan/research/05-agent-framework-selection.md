# 05 · 多Agent框架选型调研（舆情专报Agent）

- 调研日期：2026-08-12
- 调研人：技术选型调研员（Claude）
- 数据来源：本地 BettaFish 代码库逐文件阅读；PyPI JSON API / GitHub API 实测（版本号、发布日期、stars）；各官方文档与社区来源逐条附 URL
- 目标场景回顾：3 个分析 agent（事实调查 / 媒体传播 / 历史洞察）各自跑"计划→搜索→总结→反思"内循环，**并行**执行，输出写入共享**论坛**（黑板）；**主持人 agent** 评审（查漏、盲点、矛盾）后引导下一轮，外循环直到放行；**综合报告 agent** 汇总生成 HTML 专报。硬性要求：多 LLM 接入（deepseek / openai / gemini / kimi / claude / 硅基流动 / 本地 / 自定义中转）、流式输出到前端、后端 FastAPI（Python）、开发者为入门水平。

---

## 0. TL;DR 选型结论

**推荐：轻量自研（asyncio + AsyncOpenAI 工厂 + FastAPI SSE），备选 LangGraph 1.x。**

一句话理由：本项目的拓扑虽然"非标准"，但**规模小且固定**（3+1+1 个 agent、两层循环、一个黑板），用纯 Python 表达只需几百行编排代码；而各框架在本项目最难的三件事上——自定义论坛事件协议、逐 agent 流式面板、多国产 provider 接入——提供的杠杆最少、摩擦最多。参考项目 BettaFish 已用自研路线验证了业务可行性（其架构缺点恰恰是可以现代化改进的部分，见 §1.3）。若后续确需断点续跑、复杂图演化或可视化调试，LangGraph 1.x（API 已稳定）是明确的第二选择，且自研时保持"纯函数节点 + 独立 LLM 客户端"结构可让迁移成本很低。

---

## 1. 本地参考：BettaFish（微舆）用了什么方案

代码库路径：`D:\a311\系统赛\全智赛\全智赛-OPC-设计与业务参考\BettaFish-main\BettaFish-main`

### 1.1 结论：完全自研，零 agent 框架

- README 自述"从0实现的创新型多智能体舆情分析系统""基于纯Python模块化设计"（README.md 第 35、55 行）。
- `requirements.txt` 中**没有** langgraph / langchain / crewai / autogen 等任何 agent 框架，LLM 接口只有 `openai>=1.3.0`（以 OpenAI 兼容格式统一接入所有厂商）。
- Web 层是 **Flask + flask-socketio**（`app.py`），不是 FastAPI（fastapi/uvicorn 在依赖里但主应用是 Flask）；三个引擎各有 Streamlit 单页应用。

### 1.2 其多 agent 机制的具体实现

| 部件 | 实现方式 | 关键文件 |
|---|---|---|
| 单个分析 agent | 手写 `DeepSearchAgent` 类 + 自定义 Node 类（`ReportStructureNode`/`FirstSearchNode`/`ReflectionNode`/`FirstSummaryNode`/`ReflectionSummaryNode`/`ReportFormattingNode`）+ `State` 对象，**同步顺序执行**，内含"搜索→总结→反思循环"（`MAX_REFLECTIONS` 控制） | `QueryEngine/agent.py`（Media/Insight 同构） |
| LLM 接入 | `openai` SDK 薄封装 `LLMClient(api_key, model_name, base_url)`，每个引擎独立配置 base_url/model，靠 OpenAI 兼容协议实现多厂商（含硅基流动） | `QueryEngine/llms/base.py`、`config.py` |
| 并行 | **进程级**：Flask 主应用用 `subprocess` 拉起 3 个 Streamlit 子应用，各跑一个引擎 | `app.py` |
| 论坛（黑板） | **基于日志文件**：各引擎经 loguru 写各自 log；`ForumEngine/monitor.py` 的 `LogMonitor` 后台线程 tail 三个 log 文件，用**正则匹配**提取 SummaryNode 输出追加到 `forum.log` | `ForumEngine/monitor.py` |
| 主持人 | 每累计 5 条 agent 发言触发一次 `ForumHost`（默认硅基流动 Qwen3-235B，OpenAI 兼容客户端），发言以 `[HOST]` 前缀写回 `forum.log` | `ForumEngine/llm_host.py` |
| 反馈回路 | 各引擎的 summary 节点在生成总结前调用 `utils/forum_reader.get_latest_host_speech()` **读文件+正则**取最新 HOST 发言注入 prompt | `utils/forum_reader.py`、`*/nodes/summary_node.py` |
| 报告 | `ReportEngine` 独立生成 HTML 专报（模板选择/章节生成/图表修复等节点） | `ReportEngine/` |

### 1.3 借鉴与避坑

- **可借鉴**：业务分解（三引擎+主持人+报告引擎与本项目需求几乎同构）；"节点类 + State"的自研分层；OpenAI 兼容 base_url 的多厂商接入方式；主持人发言注入分析 prompt 的反馈设计。
- **应避免**：把"论坛"建在**日志文件 + 正则 tail** 上——这是进程隔离逼出来的妥协，脆弱（正则匹配日志格式、文件锁、编码问题都是它代码里真实处理过的坑）。本项目单 FastAPI 进程 + asyncio 并发即可用**内存数据结构**（如 `list` + `asyncio.Event`/`Queue`）做论坛，天然支持流式推送，复杂度骤降。
- 同理，Flask+Streamlit 多进程拼接的前端架构不必继承；FastAPI + SSE/WebSocket 单进程即可。

---

## 2. 候选框架现状逐评（截至 2026-08-12）

版本号与发布日期均为本人当日经 PyPI JSON API 实测；stars 为 GitHub API 实测或子代理实测。

### 2.1 LangGraph — 表达力最强、API 已稳定的图编排框架

- **版本**：langgraph **1.2.11**（2026-08-11 发布）；**1.0 GA 于 2025-10-29**，官方承诺 1.x 期间零破坏性变更（https://changelog.langchain.com/announcements/langgraph-1-0-is-now-generally-available ）。约每 1–2 周一个 patch。https://pypi.org/project/langgraph/
- **活跃度**：约 39.5k stars，最后 push 2026-08-11（GitHub API 实测）。1.0 后定位为 LangChain `create_agent` 之下的底层编排运行时，文档并入 docs.langchain.com。
- **本项目拓扑**：候选中**原生表达度最高**——内循环=subgraph 内条件边/`Command(goto=...)` 成环；论坛=父子图共享 state key + `Annotated[list, operator.add]` reducer（append-only 黑板）；并行=同 superstep 多出边自动并发 / `Send` 动态 fan-out；主持人=`add_node(moderator, defer=True)`（等全部并行分支完成再执行，正好是评审语义）；外循环=主持人返回 `Command(goto=...)` 决定重跑或 END。来源：https://docs.langchain.com/oss/python/langgraph/graph-api 、https://docs.langchain.com/oss/python/langgraph/use-subgraphs
- **多 provider**：`init_chat_model` 一行切换；ChatDeepSeek 官方包、ChatQwen 在列，Kimi 为社区包；硅基流动/中转走 `ChatOpenAI(base_url=...)`。https://docs.langchain.com/oss/python/integrations/chat/
- **流式**：`astream()` 多 stream mode（token 级 `messages`、`updates`、`custom` 等），v1.2 新增 event streaming API；并行子图事件**交错且无全局顺序**，前端需按 namespace 分流（https://www.abstractalgorithms.dev/langgraph-streaming-agent-responses ）。https://docs.langchain.com/oss/python/langgraph/streaming
- **FastAPI**：无官方插件但模式最成熟，官方 fullstack 模板 https://github.com/langchain-ai/langgraph-fullstack-python （FastAPI+SSE），社区生产级模板多。
- **调试**：LangGraph/LangSmith Studio **本地免费**（`langgraph dev`，需免费 LangSmith key，可关 tracing 上传）；LangSmith SaaS 免费档 5k traces/月，Plus 约 $39/席/月（定价细节未逐字核对官方页，见 §7 不确定项）。https://docs.langchain.com/oss/python/langgraph/studio
- **学习曲线**：中等偏高（State/reducer/superstep/checkpoint/子图命名空间心智模型）；**中文资料在候选中最丰富**（B站 2026 全套教程、CSDN 1.x 教程等）。
- 文档：https://docs.langchain.com/oss/python/langgraph/overview

### 2.2 OpenAI Agents SDK — 简单，但对本拓扑帮不上忙且 API 不稳

- **版本**：openai-agents **0.20.0**（2026-08-11）。**仍是 0.x**，官方版本策略明说 minor 递增=破坏性变更，0.1→0.20 已 20 个 breaking minor（如 0.20.0 的 MCP v2 迁移）。https://openai.github.io/openai-agents-python/release/
- **活跃度**：约 28.6k stars，周更节奏。2026-04 大版本新增 sandbox agents/subagents/memory 等（https://openai.com/index/the-next-evolution-of-the-agents-sdk/ ）；注意 AgentKit 的 Agent Builder/Evals 已宣布 2026-11-30 下线，官方推荐回归 SDK 写代码（https://developers.openai.com/api/docs/deprecations ）。
- **本项目拓扑**：无图/黑板原语。官方文档明示复杂编排就用 Python：while 循环 + evaluator agent + `asyncio.gather` 并行（https://openai.github.io/openai-agents-python/multi_agent/ ）——即**框架不管编排，你还是在自研**，只是内循环可用其 agent loop 代跑。handoffs/agents-as-tools 均非黑板模型。
- **多 provider**：`AsyncOpenAI(base_url=...)` 全局/逐 agent 覆盖，OpenAI 兼容端点直连顺畅；Claude/Gemini 走 LiteLLM 适配器。无 OpenAI key 需 `set_tracing_disabled()`。https://openai.github.io/openai-agents-python/models/
- **流式**：`Runner.run_streamed()` 有 token 级事件流，转 SSE 简单；社区 FastAPI 模板 https://github.com/ahmad2b/openai-agents-streaming-api 。
- **调试**：tracing 默认上传 OpenAI 平台（可关、可换 Langfuse/LangSmith 等 28 家外部 processor）。
- **判定**：上手最快，但在本拓扑下"框架提供的部分用不上、要自己写的部分它不提供"，再叠加 0.x 破坏性变更频繁——不选。
- 文档：https://openai.github.io/openai-agents-python/

### 2.3 Claude Agent SDK（Python）— 单 agent 能力强，但模型协议与运行时都不匹配

- **版本**：claude-agent-sdk **0.2.136**（2026-08-11，近乎日更）。https://pypi.org/project/claude-agent-sdk/
- **架构**：本质是**把 Claude Code CLI 当运行时**（agent loop 在 CLI 内执行）；CLI 已按平台打包进 wheel，**无需单独装 Node**，Python 3.10+ 即可（https://github.com/anthropics/claude-agent-sdk-python ）。SDK 代码 MIT，但使用受 Anthropic 商业条款约束，且官方**禁止第三方产品复用 claude.ai 订阅额度**，默认按 Console API token 计费（https://code.claude.com/docs/en/agent-sdk/quickstart ）。
- **多 provider（关键限制）**：只认 **Anthropic Messages API 协议**端点（`ANTHROPIC_BASE_URL`）。DeepSeek（https://api-docs.deepseek.com/guides/anthropic_api ）、Kimi（https://platform.kimi.ai/docs/guide/claude-code-kimi ）、硅基流动（https://docs.siliconflow.cn/cn/usercases/use-siliconcloud-in-ClaudeCode ）恰好都官方提供 Anthropic 兼容端点，可接；但 **OpenAI/Gemini 等纯 OpenAI 格式端点不能直连**，需 LiteLLM 网关转换，且高级特性在第三方模型上的兼容性无人担保。
- **本项目拓扑**：subagents 支持并行与上下文隔离，但**无黑板/图编排抽象**——外循环、论坛、汇合仍需自写 Python。https://code.claude.com/docs/en/agent-sdk/subagents
- **流式/FastAPI**：`query()` 返回 async iterator，`include_partial_messages=True` 有 token 级事件，桥 SSE 容易；无官方 FastAPI 集成。
- **判定**：适合"以 Claude 为主、要文件系统/bash 工具重型 agent"的场景；本项目要求任意厂商可切换 + 轻量搜索型 agent，CLI 黑盒运行时反而是负担——不选作主框架。
- 文档：https://code.claude.com/docs/en/agent-sdk/overview

### 2.4 AutoGen / AG2 / Microsoft Agent Framework — 一个停更、一个刚换血、一个偏 Azure

- **AutoGen**：autogen-agentchat **0.7.5**（2025-09-30 后**零发版**）；README 顶部明示 **maintenance mode**、社区托管，新用户被导向 Microsoft Agent Framework（https://github.com/microsoft/autogen ）。SelectorGroupChat/GraphFlow 本可表达本拓扑，但**新项目不应选一个官方已停止功能开发的框架**。
- **Microsoft Agent Framework（MAF）**：agent-framework **1.13.0**（2026-07-30），1.0 GA 于 2026-04（https://visualstudiomagazine.com/articles/2026/04/06/microsoft-ships-production-ready-agent-framework-1-0-for-net-and-python.aspx ）。graph-based workflows 内置 concurrent/group-chat/Magentic 编排+循环+checkpoint，对本拓扑结构化表达完整；多 provider 子包齐全（含 anthropic/gemini/ollama）。缺点：概念多（executor/edge/workflow）、文档与生态偏 Azure，对入门者偏陡、中文资料少。https://learn.microsoft.com/en-us/agent-framework/
- **AG2**：**1.0.1**（2026-07-29）。**2026-07-27 的 v1.0 是彻底换代**：新 protocol-driven 架构，Network/Hub/Channel 模型，内置 **Discussion adapter（N 方轮流发言频道，语义上就是"论坛"）** 与 feedback_loop 等模式，与本场景语义贴合度最高（https://docs.ag2.ai/docs/user-guide/network/overview/ ）；原 AutoGen 0.2 血统整体迁往 `ag2-classic`。风险：**v1.0 上线仅两周**，生态与实战案例几乎为零——观感有趣但不适合作为比赛/交付项目的地基。
- **判定**：三者均不选。MAF 是其中唯一可长期看好的，但对本项目的入门开发者性价比低于 LangGraph。

### 2.5 CrewAI — 活跃，但抽象与本拓扑错位

- **版本**：crewai **1.15.15**（2026-08-12），约 57k stars，迭代极快。https://pypi.org/project/crewai/
- **重要变化**：1.0 起**移除 LiteLLM 依赖**，改为 7 个原生 provider（openai/anthropic/gemini/azure/bedrock/snowflake/openai_compatible）+ LiteLLM 可选 fallback；deepseek/kimi/硅基流动走 `openai_compatible`。迁移期有 base_url 传递 bug 的真实案例（https://github.com/crewAIInc/crewAI/issues/5139 ）。https://docs.crewai.com/en/learn/litellm-removal-guide
- **本项目拓扑**：Flow 层（`@start/@listen/@router` + 共享 Pydantic state）能表达外循环与黑板，但**内循环必须也下沉到 Flow 手写**——等于把 CrewAI 当轻量事件引擎用，其核心卖点（role/task 的 Crew 模型）用不上。社区对"复杂分支/共享状态与抽象打架"的抱怨：https://ianas.fr/en/blog/2026/06/02/crewai-langchain-langgraph-comparatif-pragmatique/
- **流式（明确短板）**：token 流靠事件总线 `LLMStreamChunkEvent` 旁路捕获再自己转 SSE；多 agent 场景流式区分是长期未决 feature request（https://github.com/crewAIInc/crewAI/issues/2950 ），曾有 chunk 乱序 issue 被关闭为 not planned（https://github.com/crewAIInc/crewAI/issues/3008 ）。
- **判定**：不选。上手快的红利在本拓扑下兑现不了，流式恰是其弱项而是本项目硬需求。
- 文档：https://docs.crewai.com

### 2.6 MetaGPT — 已停滞，仅其"消息池"思想值得借鉴

- **版本**：**0.8.2**（2025-03-09），此后 17 个月无 release；最后 commit 2026-01-21，2026 年主库近乎零提交；团队转向商业产品 MGX 与 OpenManus 等新项目（https://github.com/FoundationAgents/MetaGPT/releases 、https://github.com/FoundationAgents/OpenManus ）。
- 其 Environment**共享消息池 + Role 订阅**机制（https://arxiv.org/html/2308.00352v6 ）与本项目"论坛"思想同源，可作设计参考，但框架本身**不建议新项目采用**（依赖陈旧、围绕软件公司 SOP 预设、停滞）。
- 文档：https://docs.deepwisdom.ai

### 2.7 轻量自研路线（asyncio + 直连 LLM API，可选 litellm）

- **多 provider 可行性：已逐家验证为"官方支持的姿势"**——一个 `AsyncOpenAI(base_url=..., api_key=...)` 工厂即可覆盖全部需求厂商：DeepSeek（https://api-docs.deepseek.com/ ）、Kimi（https://platform.moonshot.cn/docs/guide/migrating-from-openai-to-kimi ）、硅基流动（https://docs.siliconflow.cn/en/userguide/quickstart ）、Gemini OpenAI 兼容端点（https://ai.google.dev/gemini-api/docs/openai ）、Claude OpenAI 兼容层（官方标注迁移/评测用途，生产可换 anthropic SDK 或 litellm，https://docs.claude.com/en/api/openai-sdk ）、本地 Ollama/vLLM（OpenAI 兼容）与任意中转。注意：openai SDK **3.0.0 于 2026-08-12 刚发布**，各厂商长期测试基线是 2.x，**建议锁定 2.x** 待兼容性明朗。
- **LiteLLM（可选统一层）**：**1.96.2**（2026-08-11），56.1k stars，周更。DeepSeek/Moonshot 有一等 provider 文档页；**硅基流动无一等支持**（需走 openai_compatible 通道）。须知 **2026-03 供应链事件**：PyPI 1.82.7/1.82.8 被植入凭证窃取代码，官方随后加固发布链（https://github.com/BerriAI/litellm/issues/24843 ）。功能广但依赖重、迭代快偶发回归——本项目厂商全部走 OpenAI 兼容协议，**litellm 非必需**。
- **思潮佐证（2025–2026 主流工程共识）**：Anthropic《Building Effective Agents》"从直接调 API 开始，而非框架"（https://www.anthropic.com/engineering/building-effective-agents ，HN 763 分讨论 https://news.ycombinator.com/item?id=42470541 ）；12-Factor Agents（25.2k stars，https://github.com/humanlayer/12-factor-agents ）；smolagents 千行极简理念（28.8k stars，https://huggingface.co/blog/smolagents ）；Cognition《Don't Build Multi-Agents》（https://cognition.ai/blog/dont-build-multi-agents ）；Thorsten Ball《How to Build an Agent》（https://ampcode.com/how-to-build-an-agent ）。
- **流式/FastAPI**：SDK 原生 `stream=True` → `asyncio.Queue` → FastAPI `StreamingResponse`(SSE)，事件协议完全自定义——这正是"论坛前端"需要的。

---

## 3. 关键维度对比表

| 维度 | 轻量自研 | LangGraph 1.2 | OpenAI Agents 0.20 | Claude Agent SDK 0.2 | CrewAI 1.15 | AG2 1.0 | MAF 1.13 | AutoGen 0.7 | MetaGPT 0.8 |
|---|---|---|---|---|---|---|---|---|---|
| 论坛黑板+双层循环 | 完全自由（自己写） | **原生表达最佳** | 手写（框架不管） | 手写（无编排抽象） | 可但错位 | 语义最贴但太新 | 完整但概念重 | 可但已冻结 | 契合但停滞 |
| 多 provider（含国产/中转/本地） | **最顺**（base_url 工厂） | 顺（官方 DeepSeek 包等） | 顺（OpenAI 兼容为主） | **受限**（仅 Anthropic 协议端点） | 中（openai_compatible） | 顺 | 顺 | 顺 | 旧 |
| token 流式→FastAPI SSE | **最直接** | 成熟（需 namespace 分流） | 可行 | 可行 | **弱项**（事件旁路+已知 issue） | 有 Stream 系统 | 原生 | 可行 | 无一等支持 |
| API 稳定性 | 取决于自己 | **1.x 零破坏承诺** | 0.x 月度 breaking | 0.x 日更 | 1.x 快迭代 | 刚换代 | 1.x GA | 冻结 | 停滞 |
| 入门者上手 | 低门槛（只是 Python） | 中偏高 | 低 | 低（但运行时黑盒） | 低（生产期变高） | 中 | 中偏高 | 中 | 高 |
| 调试透明度 | **全透明** | Studio 本地免费可视化 | tracing 可外接 | OTel/hooks | 口碑差（黑盒 prompt） | telemetry 齐 | DevUI+OTel | — | — |
| 中文资料 | 不需要 | **最丰富** | 一般 | 一般 | 较多 | 少 | 少 | 旧 | 旧 |

---

## 4. 焦点分叉：LangGraph vs 轻量自研（对入门开发者谁的总成本更低）

### 4.1 先承认 LangGraph 的真实优势

- 本拓扑与其原语几乎一一对应（子图共享 key、Send、defer、Command 循环），是所有框架中表达最自然的；
- 1.x API 稳定承诺 + 免费本地 Studio 可视化 + 最丰富的中文教程；
- checkpoint/持久化/断点续跑是**自研很难低成本补齐**的能力。

### 4.2 但对"本项目 + 入门开发者"，自研总成本更低，判断依据：

1. **拓扑复杂度被高估了。**"3 个内循环并行 + 黑板 + 主持人门控外循环"翻译成 Python 就是：`while not approved:` 包一个 `asyncio.gather(агent1(), agent2(), agent3())`，论坛是一个带锁的 `list`，主持人是一次 LLM 调用后的分支判断。核心编排预计 200–400 行。BettaFish 用比这更笨的方式（多进程+日志文件）都跑通了业务；本项目在单进程 asyncio 里做只会更简单。
2. **框架杠杆集中在本项目用不到的地方，摩擦集中在硬需求上。**LangGraph 真正的增值（checkpoint、time-travel、human-in-the-loop 持久化、动态图）本项目 MVP 都不需要；而硬需求"逐 agent 流式面板 + 自定义论坛事件协议"在 LangGraph 里反而要处理并行子图事件交错、namespace 分流、再映射成自己的前端协议——等于先学一套事件体系，再把它翻译成自己的事件体系。自研则是论坛事件即前端事件，一步到位。
3. **调试是入门者的最大成本项。**自研栈里 bug 只有两种来源：你的代码、LLM 的输出，`loguru` + 打印即可定位。上框架后多了第三种：框架行为（reducer 合并语义、superstep 时序、子图状态隔离），入门者面对"图为什么没走到那个节点"类问题时排查成本最高。这正是 Anthropic《Building Effective Agents》与 12-factor agents 反复强调的：**框架的间接层在原型期遮蔽了真正该看清的东西——prompt 和数据流**。
4. **学习投资的复用性。**学会 asyncio、SSE、状态机式的循环控制，是通用 Python 工程能力；学 LangGraph 的 State/reducer/channel 心智模型，只在 LangGraph 里增值。对入门者，前者是更好的第一性投资。
5. **框架是杠杆还是枷锁的判别式**：拓扑标准（如 ReAct 单 agent、supervisor 分发）、需要持久化/回放、团队多人协作需统一约定 → 杠杆；拓扑自定义、事件协议自定义、单人快速迭代、需要看清每个 prompt → 枷锁。本项目全部命中后者。

### 4.3 何时应改投 LangGraph（预设升级触发条件）

- 需要**断点续跑/中途人工干预并持久化**（长时舆情追踪任务、任务恢复）；
- 图结构开始频繁演化（新增 agent 类型、动态生成分支），手写编排出现"回调地狱"苗头；
- 多人协作需要统一编排约定与可视化对齐。

**逃生舱设计**：自研时保持三条纪律，未来迁移 LangGraph 半天可完成——(a) 每个节点写成 `async def node(state) -> dict` 纯函数；(b) LLM 客户端独立成 provider 工厂模块；(c) 论坛/状态用单一 dataclass/PydanticModel 承载。这三者正是 LangGraph 节点/模型/State 的一比一对应物。

---

## 5. 推荐技术栈（自研路线落地要点）

- **编排**：Python 3.11+ asyncio；外循环 while + 轮次上限（防主持人永不放行）；内循环 for + `MAX_REFLECTIONS`（借鉴 BettaFish）。
- **LLM 层**：`openai` SDK（**锁 2.x**，3.0.0 刚发布未经各兼容端点验证）+ `AsyncOpenAI(base_url, api_key)` 工厂 + per-agent 模型配置（pydantic-settings）；Claude 若走生产建议 `anthropic` SDK 或后期挂 litellm 适配层；重试用 `tenacity`。
- **论坛**：进程内 `ForumBoard` 类（append-only list + asyncio.Lock + 订阅者 Queue 广播），替代 BettaFish 的日志文件方案；每条发言带 `{agent, round, type, content, ts}` 结构。
- **流式**：统一事件协议（如 `{event: token|speech|host|status|report, agent, data}`）→ `asyncio.Queue` → FastAPI `StreamingResponse` SSE（前端 EventSource），需要双向再上 WebSocket。
- **可观测**：loguru 结构化日志 + 每轮 state 落盘 JSON（既是调试面板也是断点雏形）；可选接 Langfuse（自托管免费）。
- **验证建议**：先做 1-agent 竖切（一个分析 agent 内循环 + 流式到页面），再横向复制到 3 agent + 主持人。

---

## 6. 版本与文档链接汇总（2026-08-12 实测）

| 框架/库 | 最新版本 | 发布日期 | 活跃度信号 | 官方文档 |
|---|---|---|---|---|
| LangGraph | 1.2.11 | 2026-08-11 | ~39.5k stars；1.0 GA 2025-10 | https://docs.langchain.com/oss/python/langgraph/overview |
| OpenAI Agents SDK | 0.20.0 | 2026-08-11 | ~28.6k stars；0.x 频繁 breaking | https://openai.github.io/openai-agents-python/ |
| Claude Agent SDK | 0.2.136 | 2026-08-11 | 近日更；CLI 内嵌 wheel | https://code.claude.com/docs/en/agent-sdk/overview |
| AutoGen | 0.7.5 | 2025-09-30 | **维护模式**，其后零发版 | https://microsoft.github.io/autogen/stable/ |
| Microsoft Agent Framework | 1.13.0 | 2026-07-30 | 1.0 GA 2026-04，活跃 | https://learn.microsoft.com/en-us/agent-framework/ |
| AG2 | 1.0.1 | 2026-07-29 | v1.0 换代仅两周；classic 分离 | https://docs.ag2.ai/ |
| CrewAI | 1.15.15 | 2026-08-12 | ~57k stars，日更级 | https://docs.crewai.com |
| MetaGPT | 0.8.2 | 2025-03-09 | **停滞**（2026 年主库近零提交） | https://docs.deepwisdom.ai |
| LiteLLM | 1.96.2 | 2026-08-11 | ~56.1k stars，周更 | https://docs.litellm.ai |
| openai（SDK） | 3.0.0 | 2026-08-12 | 刚发 major，建议锁 2.x | https://platform.openai.com/docs/libraries |

---

## 7. 不确定项（明示）

- LangSmith 定价细节（$39/席、5k traces/月）来自搜索聚合，未逐字核对官方 pricing 页原文。
- openai SDK 3.0.0（2026-08-12 发布）与各国产兼容端点的实测兼容性尚无人验证，故建议锁 2.x 属稳妥推断而非实测。
- MetaGPT"限制新建 issue"一说未直接验证；LiteLLM 精确 provider 数量仅有官方"100+"口径。
- CrewAI"能表达双层循环"与各框架拓扑表达结论基于官方文档能力核对 + 社区案例，未实际编码验证。
- Claude Agent SDK 高级特性（hooks/结构化输出）在第三方 Anthropic 兼容端点上的兼容性由第三方厂商保证，无官方承诺。
- "自研核心编排 200–400 行"为基于 BettaFish 同构实现与拓扑复杂度的工程估算，非实测。
