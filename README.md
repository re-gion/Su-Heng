# 舆情专报 Agent（M0 骨架竖切）

当前版本跑通一条最窄但完整的链路：输入公共事件 → 单个事实调查 Agent 检索与抓取 → 证据/陈述落 SQLite → 逐条核验并生成四级徽章 → SSE 实时展示 → 输出带可点击引用的 HTML 速览。进程中断后可从持久化检查点续跑。

## 本地启动（Windows PowerShell）

要求 Python 3.11+、Node.js 20+。

```powershell
Copy-Item .env.example .env
# 编辑 .env，至少填写 DEFAULT_API_KEY 与 LANGSEARCH_API_KEY

cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

cd ..\frontend
npm install
npm run build

cd ..\backend
.\.venv\Scripts\python.exe -m uvicorn yuqing.app.main:app --host 127.0.0.1 --port 8000
```

打开 `http://127.0.0.1:8000`。前端生产包由同一个 FastAPI 进程提供；开发前端可在 `frontend` 运行 `npm run dev`，其 `/api` 会代理到 8000 端口。

## 配置与诊断

- `DEFAULT_API_KEY` / `DEFAULT_BASE_URL` / `DEFAULT_MODEL`：所有 LLM 角色的默认配置。
- `LLM_VERIFIER_*`：核验器逐字段覆盖；其他角色也采用 `LLM_<ROLE>_*` 形式。
- `LANGSEARCH_API_KEY`：真实公开网络检索。
- `YUQING_DATA_DIR`：SQLite、gzip 快照和 HTML 报告目录，默认 `./data`。

```powershell
cd backend
.\.venv\Scripts\python.exe -m yuqing.scripts.llm_ping analyst_a
.\.venv\Scripts\python.exe -m yuqing.scripts.search_probe "测试事件" --fetch-first
```

第二条命令还会主动验证 `http://127.0.0.1` 被 SSRF 防线拒绝。

## 验证

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check yuqing tests
.\.venv\Scripts\lint-imports.exe

cd ..\frontend
npm test -- --run
npm run typecheck
npm run build
```

离线测试覆盖 SQLite 条件不变量、双 ID、URL 去重、SSRF、SSE 重放、D1–D11 的 100 个合法组合、报告 IR 正负例、全 snippet 端到端与进程重启续跑，不消耗真实 API 额度。

## M0 明确边界

M0 只有 `fact_investigator` 一个 Agent，不包含多 Agent 论坛、主持人、并行分析、PDF、可视化配置页、暂停/停止/删除端点和真实历史库。真实事件的 20 条人工准确性抽检、费用/耗时 dry-run 依赖有效 API 密钥和人工复核，不能由离线测试替代。

完整范围、契约和后续阶段见 [docs/方案包/README.md](docs/方案包/README.md)。
