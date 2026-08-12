# BettaFish（微舆）深度代码剖析报告

> 调研对象：`D:\a311\系统赛\全智赛\全智赛-OPC-设计与业务参考\BettaFish-main\BettaFish-main`（v1.2.1）
> 调研方式：直接阅读本地源码（未运行系统），所有结论均标注证据文件路径；不确定处明确说明。
> 调研日期：2026-08-12

---

## 0. 一句话总结

BettaFish 是一个"3 个深度研究 Agent（私有库/多模态/网页搜索）并行跑固定流水线 + 用日志文件当'论坛黑板'做松耦合协作 + 1 个主持人 LLM 定期总结引导 + 1 个重量级报告引擎（模板→布局→字数预算→逐章 JSON IR→交互式 HTML/PDF）"的舆情分析系统。其协作机制是**基于日志文件抓取的工程 hack 而非真正的消息总线**，三个引擎代码高度重复，但报告引擎（ReportEngine）和"接地气搜索词"提示词工程是全项目最有含金量的部分。

---

## 1. 多 Agent 架构与分工

系统实际有 **6 个 LLM 角色**（4 个"Agent"+2 个辅助 LLM 中间件），外加 1 个非 LLM 的爬虫系统：

| 角色 | 目录 | 职责 | 推荐模型（config.py） |
|---|---|---|---|
| Insight Agent | `InsightEngine/` | 私有舆情数据库（7 平台爬虫数据）深度挖掘 + 情感分析 | kimi-k2（Moonshot） |
| Media Agent | `MediaEngine/` | 多模态网络搜索（Bocha AI Search，含图片/模态卡） | gemini-2.5-pro |
| Query Agent | `QueryEngine/` | 国内外新闻精准搜索（Tavily） | deepseek-chat |
| Report Agent | `ReportEngine/` | 汇总三引擎报告+论坛日志，生成最终 HTML/PDF 报告 | gemini-2.5-pro |
| Forum Host（主持人） | `ForumEngine/llm_host.py` | 每 5 条 agent 发言总结一次、纠错、引导方向 | qwen-plus |
| Keyword Optimizer | `InsightEngine/tools/keyword_optimizer.py` | 把 Agent 书面化搜索词改写成网民口语词（SQL LIKE 查询用） | qwen-plus（小模型） |
| MindSpider（非 LLM Agent） | `MindSpider/` | 定时爬虫：热榜取词 → MediaCrawler 爬 7 大平台入库 | deepseek（仅关键词提取用） |

证据：`config.py` 第 44-77 行给每个角色独立配置 `*_API_KEY/BASE_URL/MODEL_NAME`。

### 1.1 三个研究 Agent 的内部结构（完全同构）

三个引擎目录结构一模一样：`agent.py + llms/ + nodes/ + prompts/ + state/ + tools/ + utils/`。每个 Agent 内部是 6 个"节点"组成的固定流水线（**不是 ReAct/工具调用循环，而是硬编码的节点顺序**）：

- `ReportStructureNode`：LLM 生成 5 个段落的报告大纲（`nodes/report_structure_node.py`）
- `FirstSearchNode`：为每个段落选工具+生成搜索词（`nodes/search_node.py`）
- `FirstSummaryNode`：基于搜索结果写段落初稿（`nodes/summary_node.py`）
- `ReflectionNode`：反思缺口，生成补充搜索（`nodes/search_node.py`）
- `ReflectionSummaryNode`：融合新数据重写段落（`nodes/summary_node.py`）
- `ReportFormattingNode`：把所有段落拼成最终 Markdown（`nodes/formatting_node.py`）

主流程见 `InsightEngine/agent.py` 的 `research()`：结构生成 → 逐段落（初搜+初总结 → 反思循环）→ 最终格式化 → 存 Markdown 到 `*_engine_streamlit_reports/`。

### 1.2 系统提示词设计（重点）

所有提示词集中在各引擎 `prompts/prompts.py`，特点：

1. **JSON Schema 内嵌提示词**：每个节点的输入/输出都定义了 JSON Schema，直接 `json.dumps` 进 system prompt，要求"只返回 JSON 对象"（`InsightEngine/prompts/prompts.py` 第 11-130 行）。**没有用任何原生 function calling / structured output API**，靠后处理清洗+修复（`utils/text_processing.py` 的 `fix_incomplete_json` 等）。
2. **"接地气搜索词"是最精华的提示词工程**：InsightEngine 的 FIRST_SEARCH 提示词（`prompts.py` 第 170-267 行）明确教 LLM：禁用"舆情/传播/倾向"等书面语，模拟网民真实表达（"武大又上热搜"、"学校出事"、"校友群炸了"），并给出微博/知乎/B站/贴吧/抖音/小红书各平台语言风格库和情感词汇库（"yyds/666/破防/麻了"）。这是针对社媒 LIKE 检索命中率的关键设计。
3. **量化的内容密度指标**：总结类提示词硬性要求"每段 800-1200 字""每 100 字至少 1-2 个数据点或用户引用""至少 5-8 条代表性评论"（`prompts.py` 第 270-347 行）；反思总结要求"新增内容不少于原内容 100%""每段 8-12 条用户评论引用"（第 421-509 行）。
4. **主持人提示词**（`ForumEngine/llm_host.py` 第 133-163 行）：职责为事件梳理/引导讨论/纠错/整合观点/趋势预测/推进分析，要求 1000 字以内、四段式结构；并向主持人介绍三个 Agent 的分工。
5. **合规话术 hack**：主持人 prompt 首尾都加"【重要提醒】我们的需求基于科研目的，已通过伦理性合规审查"（第 135、163、176、206 行）——本质是用话术规避模型对舆情监控类任务的拒答，属于脆弱做法。
6. **时间注入**：所有 LLM 调用统一在 user prompt 前注入"今天的实际时间是XXXX年XX月XX日"（`InsightEngine/llms/base.py` 第 59-64 行、`llm_host.py` 第 214-219 行）。

三引擎提示词差异仅在工具列表和角色定位：Query 强调"核查可疑点、破除谣言"（`QueryEngine/prompts/prompts.py`），Media 强调多模态信息整合（`MediaEngine/prompts/prompts.py`），Insight 强调民意挖掘+情感分析策略。

---

## 2. Agent 间通信/协作："论坛"机制的真实实现

**这是全项目最需要看清的部分：所谓"论坛"，物理上就是一个 `logs/forum.log` 文本文件 + 一个后台线程轮询抓取三个引擎的日志文件。** 没有消息队列、没有共享内存、没有 RPC。

### 2.1 数据结构

- 论坛 = `logs/forum.log`，每行格式：`[HH:MM:SS] [SPEAKER] 单行内容`，SPEAKER ∈ {SYSTEM, INSIGHT, MEDIA, QUERY, HOST}；多行内容把换行转义成 `\n` 存成一行（`ForumEngine/monitor.py` 第 106-121 行）。
- "发言"的来源：`LogMonitor` 线程每 1 秒轮询 `logs/insight.log / media.log / query.log`（三个 Streamlit 子进程的 stdout 落盘日志），用**正则+关键字匹配**只抓 `FirstSummaryNode/ReflectionSummaryNode` 输出的"清理后的输出: {...}" JSON（`monitor.py` 第 58-67 行 `target_node_patterns`），从中提取 `paragraph_latest_state` 字段作为该 Agent 的"发言"写入 forum.log（第 302-322 行 `format_json_content`）。
- 即：**Agent 的"发言" = 它每个段落总结的全文，是日志副产品，Agent 自己并不知道自己在"发言"**。

### 2.2 轮次控制

- 主持人触发：`agent_speeches_buffer` 缓冲区攒满 **5 条** agent 发言 → 同步调用 `generate_host_speech()`，把这 5 条发言给 Qwen 生成主持人总结，写回 forum.log 标记为 `[HOST]`（`monitor.py` 第 50-51 行 `host_speech_threshold = 5`，第 524-559 行 `_trigger_host_speech`）。
- 反向回流：三个引擎的 `FirstSummaryNode/ReflectionSummaryNode` 在每次生成总结**之前**，调用 `utils/forum_reader.py` 的 `get_latest_host_speech()` 读 forum.log 中**最新一条 HOST 发言**，用 `format_host_speech_for_prompt()` 包装成"### 论坛主持人最新总结…请参考其中的观点和建议"前缀拼进 user message（`InsightEngine/nodes/summary_node.py` 第 82-98 行，Media/Query 同位置相同实现）。
- **注意：Agent 之间看不到彼此的原始发言，只能通过 HOST 的二手总结间接感知**；HOST 每次也只看最近 5 条，且 `previous_summaries` 字段声明了但从未使用（`llm_host.py` 第 55 行）。

### 2.3 会话与终止条件

- 会话开始：监控到任一日志出现 `FirstSummaryNode`/"正在生成首次段落总结" → 置 `is_searching=True`、清空 forum.log 重开会话（`monitor.py` 第 614-626 行）。
- 会话结束（三选一，`monitor.py` 第 663-691 行）：
  1. 任一引擎日志被清空/变短（意味着新任务启动，Flask 启动 Streamlit 前会删日志）→ 结束并写 `=== ForumEngine 论坛结束 ===`；
  2. 连续 **7200 次轮询（约 2 小时）** 无新增内容 → 超时结束；
  3. 手动 stop（`/api/forum/stop` 路由）。
- 鲁棒性配套：多行 JSON 跨行捕获状态机、ERROR 日志块过滤、手写 JSON 引号修复状态机（`monitor.py` 第 425-522、758-837 行）——**为了从日志里可靠抠出 JSON，写了 400+ 行解析代码**，侧面说明该通信方式的脆弱。

### 2.4 多 Agent"大循环"的真相

README 描述的"5-N 循环阶段"（`README.md` 第 96-110 行）实际是**涌现式而非调度式**：三个 Agent 各自独立跑自己的固定流水线（5-6 段 × 每段 1 次初搜 + 2-3 次反思），因为每次段落总结前都会读最新 HOST 发言，且 HOST 每 5 条发言更新一次，所以形成了"研究→发言→主持→引导下一段研究"的软耦合循环。**没有全局调度器决定循环轮数，循环次数由段落数×反思数决定，天然终止。**

---

## 3. 数据源与获取

### 3.1 三类数据入口

| 引擎 | 数据源 | 实现 |
|---|---|---|
| QueryEngine | **Tavily 搜索 API**（新闻/网页/图片，6 个封装工具：基础/深度/24h/一周/图片/按日期） | `QueryEngine/tools/search.py`（TavilyNewsAgency） |
| MediaEngine | **Bocha AI Search**（多模态：网页+图片+AI 总结+"模态卡"结构化数据如天气/股票/百科）或 **Anspire Search**（`SEARCH_TOOL_TYPE` 切换） | `MediaEngine/tools/search.py`（BochaMultimodalSearch / AnspireAISearch） |
| InsightEngine | **本地 MySQL/PostgreSQL 舆情库**（MediaCrawler 爬取的 7 平台数据） | `InsightEngine/tools/search.py`（MediaCrawlerDB，5 个查询工具） |

### 3.2 中文社媒数据怎么拿（关键问题）

**答案：自建爬虫 MindSpider，底层是开源项目 MediaCrawler（git submodule），先爬后查，Agent 只查库不实时爬。**

- `MindSpider/BroadTopicExtraction/`：每天从聚合热榜 API `https://newsnow.busiyi.world`（NewsNow 开源项目的公共实例）拉取 12 个源的热榜（微博热搜/知乎热榜/B站/抖音/头条/贴吧/澎湃/财联社等，`get_today_news.py` 第 27-43 行）→ 用 DeepSeek 从新闻列表提取最多 100 个关键词（`topic_extractor.py`）。
- `MindSpider/DeepSentimentCrawling/`：把关键词喂给 **MediaCrawler 子模块**，支持 7 平台：`['xhs', 'dy', 'ks', 'bili', 'wb', 'tieba', 'zhihu']`（小红书/抖音/快手/B站/微博/贴吧/知乎，`platform_crawler.py` 第 33 行），爬帖子+评论写入数据库。注意：本地仓库中 `DeepSentimentCrawling/MediaCrawler/` 目录为空（子模块未初始化），未能查看其内部实现；MediaCrawler 本身需要登录态 Cookie/扫码，属于灰色爬虫。
- 库表结构：`bilibili_video / douyin_aweme / weibo_note / xhs_note / kuaishou_video / zhihu_content / tieba_note` 及对应 `*_comment` 表 + `daily_news` 表（见 `InsightEngine/tools/search.py` 第 208 行 search_configs；建表 SQL 在 `MindSpider/schema/mindspider_tables.sql`）。

### 3.3 InsightEngine 查询链路上的三个中间件（值得注意）

1. **关键词优化**：查询前先过 `keyword_optimizer`（小 Qwen 模型），把 1 个书面查询扩成多个口语化关键词分别查再合并（`InsightEngine/agent.py` 第 248-260 行）。
2. **聚类采样压缩**：结果 >50 条时用 sentence-transformers（paraphrase-multilingual-MiniLM-L12-v2）编码 + KMeans 聚类，每簇按热度取 top5，压到 50 条再喂 LLM（`agent.py` 第 129-188 行）——控制上下文长度的实用技巧。
3. **本地情感分析模型**：默认对所有查询结果自动跑 HuggingFace `tabularisai/multilingual-sentiment-analysis`（5 级情感、22 语言，本地推理，`SentimentAnalysisModel/WeiboMultilingualSentiment/predict.py` 第 13 行），结果并入响应。仓库还附带 4 套备选方案（BERT/GPT2 LoRA 微调、传统 ML、小 Qwen 微调，`SentimentAnalysisModel/` 各子目录），但接入 InsightEngine 的只有 multilingual 这套（`InsightEngine/tools/sentiment_analyzer.py`）。
4. **热度算法**：`search_hot_content` 用加权公式统一各平台热度：赞×1 + 评论×5 + 转发/收藏/投币×10 + 播放×0.1 + 弹幕×0.5（`search.py` 第 65-70 行权重定义）。

---

## 4. Loop / 迭代控制

### 4.1 单 Agent 内部循环（确定性有界，无智能终止）

```
research(query):
  1. 生成大纲（提示词硬性要求 5 段；Insight 版 config MAX_PARAGRAPHS=6）
  2. for 每个段落:
       初搜 → 初总结
       for i in range(MAX_REFLECTIONS):   # Insight=3, Media/Query=2
           反思生成新查询 → 搜索 → 反思总结（覆盖段落最新状态）
  3. 全文格式化 → 存盘
```

- 证据：`InsightEngine/agent.py` `_process_paragraphs/_reflection_loop`（第 564-896 行）；`MAX_REFLECTIONS` 定义在 `InsightEngine/utils/config.py` 第 25 行（3 次）、`QueryEngine/utils/config.py` 第 40 行（2 次）、`MediaEngine/utils/config.py` 第 44 行（2 次）；Streamlit 入口又硬编码覆盖为 2（`SingleEngineApp/query_engine_streamlit_app.py` 第 62 行）。
- **反思循环不看质量、不会提前退出**——固定跑满次数，即使第一轮已经很好或搜索连续空结果也照跑。搜索结果为空时只打日志继续。
- 容错：LLM/搜索 API 统一走 `utils/retry_helper.py` 的重试装饰器（指数退避），格式化失败有手动拼接兜底（`agent.py` 第 913-919 行）。

### 4.2 多 Agent 大循环

如第 2.4 节：无中心调度器。三个 Streamlit 子进程并行各自跑单 Agent 流水线；ForumEngine 线程旁路监听 + 每 5 条发言插入 HOST 引导；全部自然结束后由 ReportEngine 收尾。
- ReportEngine 判断"三引擎都出了新报告"的方式：`FileCountBaseline` 记录任务启动时三个报告目录的 .md 文件数基线，轮询对比是否每个目录都有新增（`ReportEngine/agent.py` 第 47-171 行、`flask_interface.py` `check_engines_ready`）——**用文件计数当同步信号，同样是文件系统 hack**。

### 4.3 ReportEngine 内部的重试控制（做得最细）

逐章生成循环中区分四类失败并分别处理：JSON 解析失败/结构校验失败/内容过稀/内容安全拦截，每章最多重试 `max(3, CHAPTER_JSON_MAX_ATTEMPTS)` 次；"内容过稀"失败会缓存历次尝试中字数最多的版本，重试耗尽后**以最佳稀疏版兜底并插入警告块**而不是整体失败（`ReportEngine/agent.py` 第 592-743 行）。这套"结构化异常分类+最优候选兜底"的重试设计值得抄。

---

## 5. 报告生成（ReportEngine，全项目最重的模块）

### 5.1 流水线（`ReportEngine/agent.py` `generate_report`，第 405-776 行）

```
输入：query + 三引擎 Markdown 报告 + forum.log 全文 (+ 可选自定义模板)
1. TemplateSelectionNode：LLM 从 6 个内置模板中选一个
2. 模板切片成章节 (core/template_parser.py)
3. DocumentLayoutNode：LLM 设计全局标题/副标题/hero/目录方案/主题 token
4. WordBudgetNode：LLM 做全书字数预算（每章目标字数+重点）
5. 逐章调用 ChapterGenerationNode：LLM 流式输出该章的 JSON（IR 块序列），
   经 IRValidator 校验，失败分类重试（见4.3），逐章落盘 (core/chapter_storage.py)
6. DocumentComposer 把章节装订成 Document IR (core/stitcher.py)
7. HTMLRenderer 渲染成单文件交互式 HTML (renderers/html_renderer.py, 6536 行)
8. 存盘 final_reports/*.html + IR JSON + 状态
```

- 6 个内置模板是 Markdown 大纲：企业品牌声誉/市场竞争格局/日常定期监测/政策行业动态/社会公共热点/突发危机公关（`ReportEngine/report_template/`）。
- 全程通过 Flask Blueprint `/api/report/*` 提供 SSE 流式推送（章节增量、进度、重试状态）、任务取消、模板列表、结果下载（`ReportEngine/flask_interface.py`）。

### 5.2 IR（中间表示）设计

`ReportEngine/ir/schema.py` 定义了版本化（IR 1.0）的 JSON 契约：17 种块类型 —— heading/paragraph/list/table/**swotTable/pestTable**/blockquote/**engineQuote**（引擎引用块，标注来源 Insight/Media/Query Agent）/hr/code/math/figure/**callout/kpiGrid/widget**/toc；12 种行内标记。生成、校验（`ir/validator.py`）、渲染三方对同一 Schema 对齐。**"LLM 只产 IR JSON，渲染完全确定性"的解耦是该模块最重要的架构决策。**

### 5.3 可视化方案（重要事实核查）

- **HTML 报告是交互式的，不是静态图**：widget 块承载 Chart.js 配置（widgetType 如 `chart.js/line`、`chart.js/doughnut`），渲染器把 Chart.js、chartjs-chart-sankey、wordcloud2、MathJax、html2canvas、jspdf **全部内联进单文件 HTML** 并带 CDN fallback（离线可用），末尾注水脚本实例化图表、绑定主题切换/打印/导出按钮（`renderers/html_renderer.py` 头注释及 `renderers/libs/` 目录）。实测样例 `final_reports/final_report__20250827_131630.html` 中有 3 处 `new Chart` 实例化。
- 图表数据质量防线：`utils/chart_validator.py`（结构校验+确定性修复）→ 失败再走 `chart_repair_api.py` 的 **LLM 修复兜底** → 仍失败降级为表格/段落（chapter prompt 明文规定"绝不留空"，`prompts/prompts.py` 第 337 行）。
- **但注意：图表数值本身来自 LLM 对三份 Markdown 报告的转写，没有任何机制保证数字真实可溯源**（校验只管结构合法，不管数据真伪）。
- PDF：两条路径——前端 html2canvas+jspdf 截图式导出；后端 `export_pdf.py` → `renderers/pdf_renderer.py` 用 **WeasyPrint** 从 IR 渲染，图表用 `chart_to_svg.py`（matplotlib）转成静态 SVG 矢量图，内嵌思源宋体 Base64 子集解决中文字体，另有 1410 行的 `pdf_layout_optimizer.py` 做分页优化。PDF 依赖 Pango/GTK 系统库，Windows/macOS 都写了环境自动探测（`pdf_renderer.py` 第 24-60 行）。
- Markdown 导出：`renderers/markdown_renderer.py`（994 行）+ 根目录 `regenerate_latest_md.py` 等再生脚本。

---

## 6. LLM 接入与配置

- **统一 OpenAI 兼容协议**：每个引擎各有一份几乎相同的 `llms/base.py`，用 `openai.OpenAI(api_key, base_url)` 客户端，支持流式（`stream_invoke_to_string`）与非流式，超时默认 1800s，自带重试装饰器。**任何 OpenAI 兼容端点都能接**（Moonshot/DeepSeek/阿里百炼/硅基流动/AiHubMix 中转 Gemini 等）。
- **每个角色独立三元组配置**（KEY/BASE_URL/MODEL_NAME × 7 个角色），通过 `pydantic-settings` 从 `.env` 加载（根 `config.py`），各引擎 `utils/config.py` 再定义引擎级参数（反思次数、输出目录等）。
- 前端可视化改配置：Flask `/api/config` GET/POST 直接读写 `.env` 并热重载（`app.py` 第 134-224 行）。
- 刻意的**异构模型策略**：不同 Agent 用不同厂商模型（Kimi 长文本挖掘 / Gemini 多模态+报告 / DeepSeek 搜索推理 / Qwen 小模型做主持与关键词），README 宣传这是避免同质化思维的设计。
- 未使用任何 Agent 框架（无 LangChain/LangGraph/AutoGen），全部手写。

---

## 7. 工程结构与代码质量观感

### 7.1 运行拓扑

```
python app.py (Flask :5000, flask-socketio)
 ├─ subprocess × 3: streamlit run SingleEngineApp/{insight,media,query}_engine_streamlit_app.py (:8501-8503)
 │    前端 index.html 用 iframe 加载，并以 URL 参数 ?query=...&auto_search=true 触发分析
 ├─ 线程: ForumEngine LogMonitor（轮询 logs/*.log → forum.log → 触发 HOST）
 ├─ 线程: forum.log 监听 → Socket.IO 推送前端"论坛"面板
 └─ Blueprint: /api/report/* （ReportEngine, SSE 流式）
```

- 前后端：后端 Flask + Flask-SocketIO + eventlet；三个 Agent 的 UI 是 Streamlit 嵌 iframe；主前端是**单文件 5936 行的 `templates/index.html`**（内嵌 Base64 字体，无前端框架、无构建流程）。
- 部署：Dockerfile + docker-compose（app + postgres:15），卷挂载日志/报告/.env（`docker-compose.yml`）。
- 测试：`tests/` 有 13 个文件，覆盖 monitor 解析、重试、路由、PDF 路径等工具性单测；**无 Agent 流程/提示词回归/报告质量的任何测试**。CI 在 `.github/workflows/`。

### 7.2 代码质量观感（直言）

优点：中文注释密度极高、模块职责命名清晰、错误兜底意识强（重试/降级/兜底随处可见）、配置集中、日志规范（loguru）。

问题：
1. **三引擎约 90% 代码重复**：`nodes/ state/ llms/ utils/` 三份近拷贝，改一处要同步三处（对比三个 `summary_node.py` 第 27-98 行几乎逐行相同）。
2. **`sys.path.append` 满天飞**（几乎每个文件头部），无包管理规范。
3. 进程间通信全靠**文件系统副作用**：日志抓取当消息总线、文件计数当就绪信号、URL 参数当任务下发；`app.py` 的 `/api/search` 会向 Streamlit 端口 POST `/api/search`（第 1173-1191 行），但 Streamlit 并不提供该端点，真实触发走的是 iframe URL 参数（`index.html` 第 3061 行）——**该路由疑似残留死代码（未运行验证，但两条路径矛盾是确定的）**。
4. `app.py` SECRET_KEY 硬编码、Socket.IO `cors_allowed_origins="*"`、`.env` 可被前端接口直接改写——演示可用，公网部署有安全隐患。
5. 单文件巨型模块：`html_renderer.py` 6536 行、`index.html` 5936 行、`chapter_generation_node.py` 2032 行。

---

## 8. 判断：可借鉴 vs 可超越

### 8.1 值得直接借鉴的设计

1. **"结构化深度研究"流水线骨架**：大纲→逐段(搜索→总结→反思×N)→汇编，配 JSON Schema 约束的节点化实现。简单、可控、效果可预期，比自由 ReAct 循环更稳，适合小预算项目。
2. **接地气搜索词提示词工程**（`InsightEngine/prompts/prompts.py`）：平台语言风格库+网络情感词汇库+反书面语规则，是中文社媒检索命中率的实战经验结晶，可整体移植。
3. **IR 中间表示 + 确定性渲染**的报告架构：LLM 只产受 Schema 约束的 JSON，HTML/PDF/MD 渲染完全确定性，配"校验→分类重试→最优候选兜底→降级为表格"的四层防线。这是做高质量报告产品的正确形状。
4. **章节级流式 SSE + 逐章落盘**：报告生成过程实时可见、断线可补发、失败可恢复（`flask_interface.py` 事件历史 deque + `chapter_storage.py`）。
5. **异构小模型中间件**：关键词优化用小 Qwen、情感分析用本地 HF 小模型、聚类采样用 MiniLM——把不需要大模型的环节换成便宜/免费组件，直接对齐"小额预算"约束。
6. **主持人机制的理念**（非其实现）：定期由一个中立 LLM 总结各 Agent 进展并注入后续生成，用极低成本实现跨 Agent 信息融合与去同质化。
7. **每角色独立 OpenAI 兼容三元组配置** + 前端可改 `.env`：自部署友好。
8. 报告模板库按业务场景分 6 类（危机公关/品牌/竞争…）+ LLM 自动选模板。

### 8.2 明显短板 / 可超越点（均可验证）

1. **"论坛"是日志抓取 hack，不是真通信**：靠正则从 stdout 日志抠 JSON（`monitor.py` 400+ 行解析/修复代码），Agent 间不能互看原文、只能读 HOST 最新一条二手总结；日志格式一变即断。→ 超越：用真正的共享消息存储（哪怕 SQLite/内存队列），让 Agent 可读完整讨论流、可@彼此、可被调度。
2. **无智能终止/预算控制**：反思固定跑满 2-3 轮，不评估增益；论坛超时靠 2 小时死数（`monitor.py` 第 680 行）；无 token/费用统计。→ 超越：反思前判断信息增益、全局 token 预算器、可中断恢复。
3. **图表数据无溯源**：最终报告的 Chart.js 数值由 LLM 转写产生，校验只保证结构合法不保证数字真实（`chart_validator.py` 职责边界）；引用的评论/数据没有回链到原始帖子 URL 的机制。→ 超越：图表数据强制来自结构化数据管道（真实统计），报告内嵌可点击的原始证据链接。
4. **私有库依赖重**：InsightEngine 价值完全依赖自建 MediaCrawler 爬虫（需要登录 Cookie、法律灰色、部署重），无库时该 Agent 无用；无"无数据库降级模式"。→ 超越：对 C 端演示站提供纯开放数据模式（RSS/API/公共热榜），私有库做可选增强。
5. **三引擎代码三份拷贝**、`sys.path` hack、多进程 Streamlit iframe 拼贴 UI。→ 超越：单一 Agent 框架抽象 + 现代前端（或至少统一 SPA），一套 nodes 多套工具配置。
6. **LLM 输出全靠"请只返回 JSON"+手写修复**，未用 function calling/JSON mode。→ 超越：原生 structured output，可删掉大量清洗代码并降低重试率。
7. **报告串行逐章生成**（`ReportEngine/agent.py` 第 569 行 for 循环），全流程慢；三引擎虽并行但各自内部段落也是串行。→ 超越：无依赖章节/段落并发生成。
8. **无历史洞察维度**：InsightEngine 只查"库里有什么"，没有事件对比、周期分析、历史相似案例检索等真正的"历史洞察"能力——这正是你规划的"历史洞察 Agent"可差异化的空档。
9. **无任何报告质量评估**：无 LLM-as-judge、无事实核查节点（Query prompt 里只有一句"破除谣言"的口头要求）、tests 不覆盖内容质量。
10. **合规靠话术**（"科研目的已通过伦理审查"反复注入 prompt）而非架构设计；对 C 端产品需要真正的合规与内容安全层。

### 8.3 不确定处（如实说明）

- MediaCrawler 子模块本地为空目录，其反爬/登录实现未能核实（`MindSpider/DeepSentimentCrawling/MediaCrawler/`）。
- 未实际运行系统；`/api/search` 路由是否死代码是基于两条触发路径矛盾的静态判断。
- `newsnow.busiyi.world` 热榜 API 的稳定性/可持续性未验证（第三方公共实例）。
- Anspire 搜索分支（`SEARCH_TOOL_TYPE=AnspireAPI` 为默认值）在 MediaEngine 中与 Bocha 的实际切换逻辑只读了部分，细节未逐行核实。
