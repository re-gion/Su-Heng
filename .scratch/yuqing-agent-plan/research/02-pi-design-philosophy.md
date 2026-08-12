# Pi 设计哲学深度剖析 —— 面向舆情多 Agent 系统的借鉴清单

> 调研对象：本地代码库 `D:\a311\系统赛\全智赛\全智赛-OPC-设计与业务参考\pi-main\pi-main`
> （即 GitHub earendil-works/pi-mono，作者 Mario Zechner / badlogic，MIT 协议，TypeScript monorepo）
> 调研方式：直接阅读源码、package.json 依赖关系、grep 跨包 import、官方 docs 与 examples。
> 所有结论均标注了具体文件路径；无法确认的地方明确写"不确定"。

---

## 1. Pi 是什么、整体架构

### 1.1 一句话定位

Pi 是一个 **"Agent 基础设施 + 极简终端编码 Agent"** 的开源 monorepo：底层是可复用的 LLM API 层和 Agent 运行时，顶层是一个刻意保持"核心极小、一切靠扩展"的编码 Agent CLI（`pi`）。`packages/coding-agent/docs/index.md` 第一句自述：

> "Pi is a minimal terminal coding harness. It is designed to stay small at the core while being extended through TypeScript extensions, skills, prompt templates, themes, and pi packages."

### 1.2 包划分与分层（证据：各包 package.json 的 dependencies + 全库 import grep）

```
第 3 层（产品层）
  coding-agent      pi CLI 本体：交互 TUI / print / RPC / JSON 四种模式，
                    扩展系统、skills、prompt templates、themes、设置、pi packages
                    依赖: agent-core, ai, tui, protocol, client

第 2 层（运行时层）
  agent (pi-agent-core)   Agent 循环 + 状态 + 事件流 + 工具执行 + 持久化 harness
                          依赖: ai, telemetry
  server / client         实验性的远程会话（CBOR 协议）  依赖: ai+protocol / protocol
  session-backends/sqlite-node   SQLite 会话后端          依赖: ai, agent

第 1 层（能力层）
  ai (pi-ai)        统一多 provider LLM API（30+ providers），流式事件、
                    工具 schema（TypeBox）、上下文序列化、跨模型接力
                    依赖: telemetry（内部仅此一个）

第 0 层（地基层，零内部依赖）
  telemetry         厂商中立的遥测契约 + 类型化 schema（零依赖）
  tui               终端 UI 库（差分渲染），零内部依赖
  protocol          传输中立的 CBOR 协议定义（仅 typebox）
```

**依赖方向是严格单向的，用 import grep 验证过：**

- `packages/ai/src` 内部 import 只有 `@earendil-works/pi-telemetry`（21 处 pi-ai 自引用类型 + 5 处 telemetry）；
- `packages/agent/src` 只 import `pi-ai` 和 `pi-telemetry`；
- `packages/tui/src` 不 import 任何内部包（grep "earendil" 只命中注释和 Symbol 名）；
- `packages/coding-agent/src` import 统计：pi-tui 67 处、pi-ai 62 处、pi-agent-core 33 处、pi-protocol 2 处、pi-client 1 处——只向下依赖，下层从不 import 上层。

即：**UI 库（tui）和 LLM 层（ai）互相完全不知道对方存在，只在最顶层 coding-agent 被组合**。这是教科书级的分层：横向解耦、纵向单向。

### 1.3 版本与工程治理

- 所有包 lockstep 同版本发布（AGENTS.md "Releasing" 节）；
- 外部依赖全部 pin 精确版本，lockfile 视为受审代码（根 README "Supply-chain hardening"）；
- `AGENTS.md` 是写给"人和 Agent 共读"的开发规则（对话风格、代码质量、git 纪律、测试方式），Pi 用自己开发自己（dogfooding）。

---

## 2. "原子—分子—材料"式开发在代码里的体现

Pi 没有用这个术语，但代码组织完全符合这个模型：

### 2.1 原子（最小不可再分单元）

| 原子 | 定义处 | 形态 |
|---|---|---|
| Tool | `packages/agent/src/types.ts` 的 `AgentTool` | `{name, label, description, TypeBox parameters, execute(toolCallId, params, signal, onUpdate)}`，错误用 throw，进度用 onUpdate 流式上报 |
| Message / 流事件 | `packages/ai/src/types.ts` | `text_delta / thinking / tool_call / usage / stop` 等标准化事件，所有 provider 归一到同一事件词表 |
| TUI 组件 | `packages/tui/src/components/` | text、box、stack、select-list、markdown 等 17 个独立组件文件 |
| Skill | 一个 `SKILL.md`（frontmatter: name+description） | 纯数据，零代码 |
| Prompt Template | 一个 `.md` 文件（`packages/coding-agent/docs/prompt-templates.md`） | 文件名即 `/命令名`，支持 `$1 $@ ${1:-default}` 参数 |
| Extension | 一个 `.ts` 文件，default export 一个工厂函数 | 收到 `ExtensionAPI` 后注册工具/命令/事件监听 |

### 2.2 分子（原子的第一次组合）

- **Agent 类**（`packages/agent/src/agent.ts` + `agent-loop.ts`）= 状态 + 循环 + 工具执行 + 事件订阅。事件序列（agent_start → turn_start → message_* → tool_execution_* → turn_end → agent_end）在 `packages/agent/README.md` 有完整时序图。
- **内置工具集**：agent 包 harness 只带 bash/edit/read/write 四个（`packages/agent/src/harness/tools/index.ts`）；coding-agent 有自己的工具实现并加了 grep/find/ls（`packages/coding-agent/src/core/tools/`），每个工具都是 `createXTool(options)` 工厂 + 可注入的 `XOperations` 接口（用于容器化时替换底层文件/进程操作）。
- **系统提示词构建器** `buildSystemPrompt()`（`packages/coding-agent/src/core/system-prompt.ts`，163 行）：纯函数，把"基础 prompt + 工具单行说明 + guidelines + 追加文本 + 项目上下文文件 + skills XML + cwd"拼成最终 prompt——每个部分都是可选注入的数据。

### 2.3 材料/组织（面向用户的成品）

- **AgentSession**（`packages/coding-agent/src/core/agent-session.ts`，3342 行）：组合根，把 ModelRuntime、SessionManager、ResourceLoader（扩展/skills/prompts/themes 发现）、压缩、系统提示词全部装配起来。
- **四种模式**复用同一个 AgentSession：interactive TUI / print / RPC(stdin-stdout JSONL) / JSON 事件流（`packages/coding-agent/src/modes/`）。
- **最有说服力的证据——subagent 多智能体是一个"材料级"扩展，不是核心功能**：`packages/coding-agent/examples/extensions/subagent/` 用 1 个扩展文件（index.ts + agents.ts）+ 4 个 markdown Agent 定义（scout/planner/reviewer/worker，frontmatter 声明 name/description/tools/model，正文即系统提示词）+ 3 个工作流 prompt（`/implement` = scout→planner→worker），实现了并行/链式子 Agent 编排——每个子 Agent 是独立 `pi` 子进程，上下文隔离。**核心一行没改。**

### 2.4 组合的关键手法

1. **工厂函数 + 选项对象**（createBashTool(options)、createAgentSession(options)），不用继承；
2. **一切能力先定义成数据（markdown/JSON），再由极薄的代码加载**——Agent 定义、Skill、Prompt 模板、主题全是文件；
3. **默认工具集只有 4 个**（system-prompt.ts 第 81 行 `["read", "bash", "edit", "write"]`），组合的自由度留给上层。

---

## 3. 递进式结构 / 渐进披露如何落实

### 3.1 对模型的渐进披露（上下文经济学）

- **Skills 机制是范本**：启动时只扫描 SKILL.md 的 frontmatter，系统提示词里只放 `<skill><name/><description/><location/></skill>` XML（`formatSkillsForPrompt`，`packages/coding-agent/src/core/skills.ts` 335-361 行）；任务匹配时模型自己用 read 工具加载全文。`docs/skills.md` 原话："This is progressive disclosure: only descriptions are always in context, full instructions load on-demand."
- **系统提示词本身也是渐进的**：默认 prompt 极短，只嵌入文档的**路径**并注明"仅当用户问到 pi 自身时才去读"（system-prompt.ts 131-138 行）——文档不进上下文，路径进上下文。
- **压缩（compaction）设计成自包含检查点**：压缩条目带 summary + retainedTail，"Context never reads past a compaction"（`packages/agent/docs/harness.md` §2.1），以及"append-only context invariant"——上下文只在尾部增长以保住 KV cache（§2.5）。

### 3.2 对用户的渐进披露（配置分层）

- 设置三级覆盖：全局 `~/.pi/agent/settings.json` → 项目 `.pi/settings.json`（需先信任项目）→ CLI flags；
- 资源发现走**约定目录**（`extensions/`、`skills/`、`prompts/`、`themes/`），零配置可用；进阶用 package.json 的 `pi` manifest 声明 + glob 过滤 + `+/-` 精确增删（`docs/packages.md`）；
- 分发递进：本地文件 → 本地目录 → git → npm 包（`pi install npm:xxx`），同一套资源规则。

### 3.3 对开发者的渐进披露（API 分层）

SDK 使用难度呈梯度（`docs/sdk.md`）：
`createAgentSession()` 一行默认 → 传 options 覆盖 → 自定义 ResourceLoader → AgentSessionRuntime（换会话/重建 cwd 状态）→ 直接用 pi-agent-core 的 Agent/agentLoop → 最底层 pi-ai 的 stream。每一层都可独立使用。

### 3.4 文档组织

- 每包一个 README（快速上手）+ coding-agent 下 30 个专题 doc 文件（quickstart → usage → customization → programmatic → reference 分区，见 `docs/index.md`）；
- `examples/extensions/` 有 50+ 个从 hello.ts 到 plan-mode/、subagent/ 的渐进示例，examples 是一等公民教材；
- 仓库自身的 `.pi/` 目录放着自用 prompts（cl/is/pr/sa/wr）和 skills（add-llm-provider.md 是一份"给 Agent 看的加 provider 检查清单"）——**用自己的机制管理自己的开发流程**。

---

## 4. Agent 基础设施：提示词、工具、扩展/skill 的定义与加载

### 4.1 系统提示词

- 纯函数 `buildSystemPrompt(options)` 组装（见 2.2），custom prompt 可整体替换但项目上下文/skills 仍会追加；
- 项目上下文文件（AGENTS.md 等）以 `<project_instructions path="...">` 包裹注入；
- 工具在 prompt 里只出现"一行说明"（toolSnippets），细节靠工具自身的 JSON schema description。

### 4.2 工具

- 契约见 2.1 原子表；补充：支持 `executionMode: "parallel" | "sequential"`（单工具可强制全批次串行）、`terminate: true` 提前终止提示、`beforeToolCall/afterToolCall` 钩子可拦截/改写（`packages/agent/README.md`）；
- 参数用 TypeBox schema 定义，执行前自动校验；
- coding-agent 的工具通过 `XOperations` 接口把真实 IO 抽出来，便于容器化替换（`packages/coding-agent/src/core/tools/bash.ts` 的 `BashOperations`）。

### 4.3 扩展（代码级插件）

- 位置约定：`~/.pi/agent/extensions/*.ts`（全局）、`.pi/extensions/*.ts`(项目，需信任)、settings、CLI `-e`；
- 通过 **jiti** 直接加载 TypeScript，无编译步骤，支持 `/reload` 热重载（`packages/coding-agent/src/core/extensions/loader.ts`）；
- `ExtensionAPI`（`packages/coding-agent/src/core/extensions/types.ts`，1728 行）提供约 35 个生命周期事件：session_*、agent_*、turn_*、message_*、tool_execution_*、`tool_call`（可返回 `{block: true, reason}` 拦截）、`context`（改上下文）、`before_provider_request`、`input` 等；注册面：registerTool / registerCommand / registerShortcut / registerFlag / registerProvider / registerMessageRenderer / registerEntryRenderer；
- 扩展跑在主进程、拥有完整系统权限（文档明确警告），Pi 把安全边界交给容器（`docs/containerization.md`）。

### 4.4 Skill（数据级插件）

- 实现 Agent Skills 标准（agentskills.io），加载器在 `packages/coding-agent/src/core/skills.ts`：递归找 SKILL.md、解析 frontmatter、校验 name/description（宽松：多数问题只 warning 仍加载，唯缺 description 拒载）、同名冲突"先到先得 + 诊断记录"、支持 .gitignore 过滤、可直接挂 Claude Code / Codex 的 skills 目录；
- `disable-model-invocation: true` 可让 skill 只能通过 `/skill:name` 手动触发，不进系统提示词。

### 4.5 持久化 harness（重量级基建）

`packages/agent/docs/harness.md`（2942 行实现规范）定义了可崩溃恢复的 Agent 运行时：
- **三存储**：append-only 会话树（entries）+ 可变寄存器（registers）+ 只增用量账本（usage ledger）；
- **持久程序计数器**：每步事务性覆写 `op.state`，恢复=读一个寄存器然后 switch，不回放日志；
- **效果三明治**：`commit 意图（预留输出 id）→ 执行不确定的外部效果 → commit 结算`，工具声明 `replay: "never"|"safe"` 决定崩溃后重放还是补合成错误结果；
- **lanes**：同一会话树上的多游标，支撑并行线程/子 Agent 共享历史。
- 三种后端（Memory/JSONL/SQLite）过同一套 conformance 测试。

---

## 5. 可直接搬用 / 需改造的模式清单（Python FastAPI + React 舆情多 Agent 系统）

> 前提说明：Pi 是 TypeScript 单机 CLI，你的项目是 Python 后端 + Web 前端的自部署服务。**哲学层面几乎全部可迁移，实现层面需要逐条判断。**

### A. 可直接搬用（哲学 + 结构照搬，换语言实现）

| # | 模式 | Pi 证据 | 迁移到你的项目 |
|---|---|---|---|
| A1 | **严格单向分层**：llm 层 → agent 运行时 → 产品层；UI 与 LLM 层互不相识 | 1.2 节 import 统计 | Python 包划分 `llm/`（provider 抽象）→ `agent_core/`（循环+事件）→ `app/`（FastAPI+舆情业务）；用 import-linter 在 CI 强制依赖方向 |
| A2 | **Agent = 类型化事件流**，UI 只消费事件 | `packages/agent/README.md` 事件时序 | 定义同款事件枚举（agent_start/turn/message_delta/tool_execution_*），FastAPI 用 SSE/WebSocket 推给 React；前端天然拿到流式进度 |
| A3 | **子 Agent 定义 = markdown 文件**（frontmatter: name/description/tools/model + 正文即系统提示词） | `examples/extensions/subagent/agents/*.md` | 舆情采集员/分析师/报告撰写员各一个 .md，改提示词不改代码、可版本管理、可让用户自定义 Agent |
| A4 | **Skill 渐进披露**：提示词只放 name+description+路径，全文按需 read | `core/skills.ts` + `docs/skills.md` | 舆情报告模板、各平台采集要领、行业术语表做成 SKILL.md；控制上下文成本 |
| A5 | **Prompt 模板 = .md + 位置参数** | `docs/prompt-templates.md` | 日报/周报/专项报告的触发模板，`/daily-report 品牌名` |
| A6 | **系统提示词 = 纯函数组装的数据**，各段可选注入 | `core/system-prompt.ts` | 写一个 `build_system_prompt(tools, guidelines, context_files, skills, append)`，禁止在业务代码里散落拼 prompt |
| A7 | **AgentMessage ≠ LLM Message**：历史里可存自定义消息类型，`convertToLlm` 过滤/转换后才发给模型 | `packages/agent/README.md` "Custom Message Types" | 舆情数据卡片、来源引用、审核记录存进会话历史但不喂给 LLM（或转成摘要再喂） |
| A8 | **工具契约**：schema 校验参数（TypeBox→pydantic）、错误用异常、onUpdate 流式进度、parallel/sequential 执行模式 | `AgentTool` 定义 | 爬虫、检索、情感分析全按此契约写；长任务（采集）用进度回调驱动前端进度条 |
| A9 | **before/after tool call 钩子可拦截** | `agent.beforeToolCall` + 扩展 `tool_call` 事件 | 落地为"专报发布前人工确认"、敏感操作审计的人机协同闸门 |
| A10 | **配置三级覆盖 + 约定目录** | settings.json 全局→项目→CLI；`skills/` `prompts/` 约定目录 | 系统默认配置 → 部署实例配置 → 单次任务参数；skills/prompts 放约定目录热加载 |
| A11 | **文档组织**：每模块 README + 专题 docs + examples 一等公民 + AGENTS.md（给 AI 协作者的规则文件） | 根目录与各包 | 起步就建 AGENTS.md/CLAUDE.md，用 AI 开发时收益立竿见影 |
| A12 | **conformance 测试套**：多后端过同一套契约测试 | harness.md Part 9 | 你若做"内存/SQLite/Postgres"多存储或多 LLM provider，同款做法保证可替换性 |

### B. 借鉴思想、简化实现

| # | 模式 | 说明 |
|---|---|---|
| B1 | **效果三明治（意图→执行→结算）** | 完整 harness 规范（2942 行）对你是过度工程。但"昂贵外部效果（LLM 调用、批量爬取）先写意图记录再执行、完成后事务结算、崩溃后按 replay 策略恢复"这个骨架，用 Postgres 一张任务表 + 状态机就能实现，对长时舆情采集任务价值很大 |
| B2 | **会话 = append-only 事件 + 少量可变状态** | JSONL/append-only 会话存储 + 压缩（compaction 带自包含摘要）思想可用 DB 表实现；上下文只尾部增长保 KV cache 的原则直接采纳 |
| B3 | **subagent = 隔离上下文的独立进程** | Pi 用子进程隔离；你用独立的 Agent 会话对象/异步任务即可，关键是"每个子 Agent 独立上下文窗口 + 只回传压缩结论（Pi 限 50KB/任务）" |
| B4 | **渲染器注册表** | Pi 的 registerMessageRenderer/registerEntryRenderer 按消息类型注册 TUI 渲染器 → React 侧做一个"消息类型 → 组件"的 registry，舆情卡片/图表/报告各有组件 |
| B5 | **pi packages 分发机制** | npm/git 装扩展对 C 端产品太重；但"一个 zip/目录 = skills+prompts+模板"的可分享资源包概念，可作为舆情模板市场的雏形 |

### C. 不可迁移（形态差异）

- **jiti 动态加载 TS 扩展**：Python 没有等价物也不该有——让 C 端用户上传可执行代码是安全灾难；扩展点收敛为"数据文件（skill/prompt/agent 定义）+ 官方维护的 Python 插件（entry_points）"两级。
- **TUI 全套（差分渲染、keybindings、tmux 集成）**：你有 React，整包无关。
- **四种终端模式（interactive/print/RPC）**：被 FastAPI 路由 + SSE 替代，但"同一 AgentSession 被多种前端复用"的内核-外壳分离必须保留。
- **30+ LLM provider 矩阵与 OAuth 订阅登录**：YAGNI，支持 2-3 个（如 OpenAI 兼容 + 国产模型）即可；但 pi-ai 的"统一事件词表 + 跨模型上下文接力"接口设计值得抄。
- **lockstep 多包发版、npm 供应链加固**：单应用自部署用不上。

---

## 6. Pi 明确不适合借鉴的部分

1. **"无内置权限系统"的安全模型**。根 README 明说 Pi 不做文件/网络/进程权限控制，边界外包给 Docker/microVM。这对开发者 CLI 成立，对面向 C 端个人用户的自部署服务不成立——你的权限、配额、内容审核必须做在应用内。
2. **"扩展拥有完整系统权限跑任意代码"的信任模型**（docs/extensions.md 的 Security 警告）。同上，C 端产品不能继承。
3. **组合根膨胀**：`agent-session.ts` 3342 行、extensions/types.ts 1728 行——即使分层优秀，组合层也会长成大文件。你的装配层应尽早按"运行时装配/资源发现/会话生命周期"拆分（Pi 自己也拆出了 agent-session-runtime.ts / agent-session-services.ts，说明它也在还这个债）。
4. **双份工具实现的历史包袱**：agent 包 `harness/tools/`（bash/edit/read/write）与 coding-agent `core/tools/`（同名四个 + grep/find/ls）并存。从代码看这是 harness 规范重写期的过渡态（不确定，未见迁移说明），但提示：**下层库过早内置"业务工具"会导致上层重写**——你的 agent_core 不要内置舆情工具，工具全部由 app 层注入。
5. **server/client/protocol 包**：自述 "experimental"，evals 包基本为空。别把实验性远程协议当成熟设计参考。
6. **面向"自我扩展"的元设计**（pi 能给自己写扩展、文档鼓励"ask pi to build one"）。这是开发者工具的核心卖点，但舆情产品的用户不写代码，把这份预算投到模板/配置的易用性上。

---

## 附：核心证据文件索引

| 主题 | 路径（相对 pi-main/pi-main） |
|---|---|
| 分层与依赖 | `packages/*/package.json`、README.md 包表 |
| Agent 循环/事件/工具契约 | `packages/agent/README.md`、`packages/agent/src/agent.ts`、`agent-loop.ts`、`types.ts` |
| 持久化 harness 规范 | `packages/agent/docs/harness.md`（2942 行） |
| 系统提示词组装 | `packages/coding-agent/src/core/system-prompt.ts` |
| Skill 加载与渐进披露 | `packages/coding-agent/src/core/skills.ts`、`docs/skills.md` |
| 扩展系统 | `packages/coding-agent/src/core/extensions/{types,loader,runner}.ts`、`docs/extensions.md` |
| 多 Agent 编排范例 | `packages/coding-agent/examples/extensions/subagent/` |
| SDK 分层 | `packages/coding-agent/docs/sdk.md`、`src/core/agent-session.ts` |
| 资源打包/分发 | `packages/coding-agent/docs/packages.md` |
| 工程治理与 dogfooding | `AGENTS.md`、`.pi/prompts/`、`.pi/skills/add-llm-provider.md` |
