# 12 技术栈与工程结构确认

Type: grilling
Status: resolved
Blocked by: 05, 09

## Question

确认或修正暂定技术栈（前端 React+Next.js；后端 FastAPI+LangGraph——框架部分以09的决策为准）；定工程目录结构（吸收Pi的分层/原子化哲学）；多LLM接入与配置系统设计（deepseek/openai/gemini/kimi/minimax/claude/硅基流动/本地/自定义中转，前端可配）；搜索API多家兜底的配置方式；后端到前端的流式输出方案。

## Answer

用户决策（2026-08-12）：**前端=Vite+React SPA，替换PRD暂定的Next.js**——构建产物由FastAPI静态托管，部署=一个Python进程，服务"零门槛"卖点；本地工具无SSR/SEO需求，Next.js属不必要的重。后端按09决议：FastAPI+轻量自研编排。配套设计（细节在SPEC票落实）：

1. **工程结构**：单仓 `backend/`（Python，分层：core基建原子→agents→services→api，学Pi单向依赖，import-linter强制）+ `frontend/`（Vite+React+TS）+ `docs/`。Agent定义走数据文件（markdown/yaml：系统提示词+工具清单+模型角色），学Pi"能力先定义成数据"。
2. **多LLM配置**：按agent角色配独立三元组（KEY/BASE_URL/MODEL，OpenAI兼容直连，学BettaFish但简化角色数）；.env为底、前端设置页可读写（配置API+本地存储）；默认配置开箱即跑（免费搜索链+单一低成本LLM也能完整出报告，角色可渐进细分）。
3. **搜索API兜底**：SearchProvider抽象+适配器（LangSearch/千帆/智谱/博查/Tavily/Serper…）+优先级链+配额感知降级（03调研的组合策略落地）；原文抓取独立FetchProvider（Jina/Firecrawl/trafilatura兜底）。
4. **流式方案**：统一SSE事件协议（事件类型：agent状态、论坛发言、核验进度、报告章节等），前端做"事件类型→组件"渲染注册表（学Pi）。
5. **可视化库**：候选ECharts（中文生态好）vs Chart.js（BettaFish实证可行），以11样张实际效果定稿。
6. **存储**：单SQLite（证据库、claim、任务state、热榜快照、配置），零外部依赖——对比BettaFish强制MySQL的部署痛点。
