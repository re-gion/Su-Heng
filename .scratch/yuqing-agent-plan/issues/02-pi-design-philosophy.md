# 02 Pi设计哲学提炼

Type: research
Status: resolved

## Question

Pi Agent 的设计哲学（分层架构/单向依赖、原子-分子-材料式开发、递进式结构/渐进披露）在代码层面具体是怎么落实的？它的agent基础设施（系统提示词、skill、工具、扩展机制）是如何组织的？对本舆情agent项目的工程结构和agent基础设施设计，有哪些可以直接搬用或改造的模式？

代码库位置：`D:\a311\系统赛\全智赛\全智赛-OPC-设计与业务参考\pi-main\pi-main`

调研成果写入：`.scratch/yuqing-agent-plan/research/02-pi-design-philosophy.md`

## Answer

完整报告：`../research/02-pi-design-philosophy.md`（证据文件路径见报告）。要点：

1. Pi 是"Agent 基建 + 极简编码 CLI"的 TS monorepo：telemetry/tui/protocol（零内部依赖）→ ai（统一 30+ LLM provider）→ agent-core（循环+状态+事件）→ coding-agent（产品）。依赖严格单向，已用 import grep 验证。
2. 核心刻意极小：默认工具仅 read/bash/edit/write 四个；多智能体编排（scout→planner→worker）完全是一个示例扩展 + 4 个 markdown Agent 定义实现的，核心零改动。
3. 原子-分子-材料对应：原子=Tool/消息事件/SKILL.md/prompt 模板/单文件扩展；分子=Agent 类、工厂函数工具；材料=AgentSession 组合根 + 四种模式复用同一内核。组合手法是"工厂+选项对象"和"能力先定义成数据文件"。
4. 渐进披露三层落地：对模型——skills 只把 name+description 放系统提示词，全文按需读；对用户——全局→项目→CLI 三级配置；对开发者——SDK 从一行创建到裸 agentLoop 逐层可下钻。
5. Agent 基建：系统提示词是纯函数组装的数据；工具契约=schema 校验+流式进度+并行/串行+before/after 钩子；扩展约 35 个生命周期事件；另有崩溃恢复 harness（持久程序计数器+"意图→执行→结算"效果三明治）。
6. 建议直接搬用（换 Python/React 实现）：单向分层（import-linter 强制）、类型化事件流经 SSE 推 React、子 Agent 定义=markdown frontmatter、skills 渐进披露、AgentMessage 与 LLM Message 分离、工具契约（pydantic）、tool_call 拦截钩子做专报发布人工闸门、AGENTS.md 工程规则文件。
7. 借鉴思想但简化：效果三明治用一张任务状态表实现（对长时采集任务价值大）；子 Agent 隔离上下文+只回传压缩结论；React 侧做"消息类型→组件"渲染注册表。
8. 不可迁移：jiti 动态加载用户代码（C 端安全灾难）、TUI 全套、30+ provider 矩阵（支持 2-3 个即可起步）。
9. 明确不学：Pi 无内置权限系统、扩展全权限跑任意代码——C 端产品必须把权限/配额/审核做进应用内。
10. 反面教训：组合根膨胀到 3342 行——装配层要提早拆；底层库不要内置业务工具，全部由 app 层注入。
11. 不确定项：双份工具实现判断为 harness 重写过渡态，属推断。
