# 舆情专报 Agent（V1.5 数据纵深）

V1.5 在完整参赛链路上补齐数据纵深：历史数据集与热榜快照进入本地 SQLite，历史洞察 Agent 本地优先、检索兜底；报告新增可追溯历史卡片和真实热度曲线，并可导出同源 HTML/PDF 与证据包 ZIP。分析过程通过 SSE 实时展示，进程中断可从检查点续跑。

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
6. 报告页可直接下载 PDF 和证据包；ZIP 内包含报告、manifest 与去脚本后的证据快照，链接可离线回看。

## 导入历史数据与采集热榜

通用导入器支持 JSON、JSONL、CSV，仅保留事件名、时间、摘要、结局、性质、五维标签、来源链接等白名单字段，并记录数据资产来源和权利说明：

```powershell
cd backend
.\.venv\Scripts\yuqing-import-dataset.exe --file .\events.jsonl `
  --slug public-events --name "公开舆情事件集" `
  --source-url "https://example.org/dataset" --license-label "CC-BY-4.0" `
  --rights-note "已核对再分发条件" --redistribution allowed
```

微博热搜历史仓库可按日期一键导入。该仓库代码采用 MIT，但热榜内容的再利用权利仍需部署者自行核对，因此命令要求显式确认：

```powershell
cd backend
.\.venv\Scripts\yuqing-import-weibo-hot-history.exe `
  --from 2024-05-20 --to 2024-05-22 --acknowledge-upstream-rights
```

配置 `YUQING_HOTLIST_URLS` 后，应用会定时采集 DailyHotApi 兼容源；也可单次执行 `yuqing-collect-hotlist`。只有数据库实际覆盖到目标事件时，报告才画热度曲线，不用搜索结果数冒充热度。

## 演示部署

```powershell
Copy-Item .env.example .env
# 填写模型与搜索 Key
docker compose -f compose.demo.yml up --build -d
```

打开 `http://127.0.0.1:8080`。演示模式默认禁用在线改 Key，按 HttpOnly 浏览器会话限制每日任务数和同时运行数，任务、报告及下架操作同样按会话隔离，并按 TTL 清理。公网部署仍应在 Caddy 前增加 HTTPS、正式身份认证、受控出口和运维监控。

## 配置规则

- `DEFAULT_API_KEY / BASE_URL / MODEL`：七个角色的默认三元组。
- `LLM_<ROLE>_*`：按字段覆盖；角色为 `ANALYST_A/B/C`、`MODERATOR`、`VERIFIER`、`REPORTER`、`UTILITY`。
- `SEARCH_PROVIDER_ORDER`：搜索顺序；支持 LangSearch、智谱、百度千帆、Tavily、Serper。未配置 Key 的项自动跳过，失败会熔断并降级。
- `YUQING_HOTLIST_URLS / PLATFORMS / INTERVAL_SECONDS`：热榜数据源、平台名和采集周期。
- `YUQING_DEMO_*`：演示站只读、并发/每日额度、会话 Cookie 与报告 TTL；启用 HTTPS 时设置 `YUQING_DEMO_COOKIE_SECURE=true`，生产内网使用时保持 `YUQING_DEMO_MODE=false`。
- `YUQING_PDF_BROWSER`：可选的 Edge/Chromium 可执行文件；Windows 会自动发现 Edge，镜像已内置 Chromium。
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

离线回归覆盖存储条件不变量、SSE 无缺无重、论坛恢复、三 Agent 故障隔离、主持人结构降级、搜索链熔断、任务控制、核验决策表、历史数据溯源、热榜去重、IR 迁移、PDF/证据包和演示治理。

## 诚实边界

- V1.5 只处理公开材料；不抓登录态评论，不做舆情走向预测，不输出无来源的情感百分比。
- 创建任务前会由 `utility` 角色执行公共性门禁；针对可识别普通个人或未成年人的私人指控会拒绝创建任务。
- 报告图表只统计本次证据库，不代表全网绝对声量；样本不足会降级为文字并进入局限性声明。
- 本地历史库只是辅助证据，未命中时仍以公开网页检索为主；导入者必须自行确认数据许可、个人信息和再分发边界。
- HTML 自包含且离线可打开；PDF 与 HTML 共用同一份报告 IR，证据包仅收录已清洗快照，不包含密钥和数据库。
- 真实事件的事实准确性仍需人工抽检；自动测试能保证引用闭包和合同，不能替代采编判断。
- 原文抓取已阻断直接解析到内网/本机的 URL；DNS rebinding 的校验-连接绑定仍需在生产部署用受控 egress 抓取代理进一步加固。

详细产品、架构与契约见 [方案包](docs/方案包/README.md)。
