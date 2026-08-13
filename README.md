# 舆情专报 Agent（V1 参赛 MVP）

V1 已跑通完整产品链路：输入公开事件 → 事实调查 / 媒体传播 / 历史洞察三 Agent 并行 → 论坛黑板协作 → 主持人评审与补查 → 逐条证据核验 → 十板块可核验 HTML 专报。分析过程通过 SSE 实时展示，进程中断可从检查点续跑。

## 十分钟启动（Windows PowerShell）

要求 Python 3.11+、Node.js 20+。无需 MySQL、Docker 或浏览器服务。

```powershell
Copy-Item .env.example .env
# 编辑 .env：至少填写 DEFAULT_API_KEY 与一个搜索 Key（推荐 LANGSEARCH_API_KEY）

cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

cd ..\frontend
npm install
npm run build

cd ..\backend
.\.venv\Scripts\python.exe -m uvicorn yuqing.app.main:app --host 127.0.0.1 --port 8000
```

打开 `http://127.0.0.1:8000`。开发前端可在 `frontend` 运行 `npm run dev`，`/api` 会代理到 8000 端口。

## 怎么使用

1. 输入事件名称，可补充时间范围和特别关注点，选择快速 / 标准 / 深入档。快速档只启用事实调查 Agent；标准与深入档启用三路调查。
2. 观察三个调查席、论坛原始发言、主持人缺口评审、证据台账与预算条。
3. 分析完成后打开 HTML 专报，在速览 / 完整版之间切换，按徽章筛选并点击引用回看证据卡。
4. 任务异常中断后，从历史任务点“续跑”；运行中可暂停，或“停止并出报告”。
5. 右上角进入配置中枢，配置七个 LLM 角色和搜索降级链，并逐项测试连接。

## 配置规则

- `DEFAULT_API_KEY / BASE_URL / MODEL`：七个角色的默认三元组。
- `LLM_<ROLE>_*`：按字段覆盖；角色为 `ANALYST_A/B/C`、`MODERATOR`、`VERIFIER`、`REPORTER`、`UTILITY`。
- `SEARCH_PROVIDER_ORDER`：搜索顺序；支持 LangSearch、智谱、百度千帆、Tavily、Serper。未配置 Key 的项自动跳过，失败会熔断并降级。
- 设置页写入本地 SQLite，优先级高于 `.env`，不会反写 `.env`；所有密钥只返回脱敏值。
- 只使用一组默认模型也能完成任务，但报告会如实标记模型同源造成的核验独立性限制。

连接诊断：

```powershell
cd backend
.\.venv\Scripts\python.exe -m yuqing.scripts.llm_ping analyst_a
.\.venv\Scripts\python.exe -m yuqing.scripts.llm_ping verifier
.\.venv\Scripts\python.exe -m yuqing.scripts.search_probe "测试事件" --fetch-first
```

语义探针必须返回 `reply=OK` 才算 LLM 可用；仅 HTTP 成功不算。搜索探针还会验证内网 URL 被 SSRF 防线拒绝。

## 质量与验证

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\lint-imports.exe

cd ..\frontend
npm test
npm run typecheck
npm run build
```

离线回归覆盖存储条件不变量、SSE 无缺无重、论坛恢复、三 Agent 故障隔离、主持人结构降级、搜索链熔断、任务控制、核验决策表、十板块 IR、真实口径图表和全 snippet 路径。

## 诚实边界

- V1 只处理公开材料；不抓登录态评论，不做舆情走向预测，不输出无来源的情感百分比。
- 创建任务前会由 `utility` 角色执行公共性门禁；针对可识别普通个人或未成年人的私人指控会拒绝创建任务。
- 报告图表只统计本次证据库，不代表全网绝对声量；样本不足会降级为文字并进入局限性声明。
- HTML 自包含且离线可打开；完整原文快照只在部署机可看。PDF 与证据包 ZIP 属 V1.5。
- 真实事件的事实准确性仍需人工抽检；自动测试能保证引用闭包和合同，不能替代采编判断。
- 原文抓取已阻断直接解析到内网/本机的 URL；DNS rebinding 的校验-连接绑定仍需在生产部署用受控 egress 抓取代理进一步加固。

详细产品、架构与契约见 [方案包](docs/方案包/README.md)。
