# 01 BettaFish深度剖析

Type: research
Status: resolved

## Question

BettaFish 是如何实现舆情分析的？逐项搞清：多agent架构与分工、agent间通信/协作机制（尤其它的论坛或类似机制）、数据源与获取方式（用了哪些API/爬虫）、Loop/迭代控制、报告生成（内容结构、可视化、导出）、LLM接入与配置方式、工程目录结构。然后判断：哪些值得直接借鉴？哪些是明显短板或可超越点？与本项目初步PRD的相似点、差异点是什么？

代码库位置：`D:\a311\系统赛\全智赛\全智赛-OPC-设计与业务参考\BettaFish-main\BettaFish-main`

调研成果写入：`.scratch/yuqing-agent-plan/research/01-bettafish-analysis.md`

## Answer

完整报告：`../research/01-bettafish-analysis.md`（全部结论带源码路径证据；基于静态阅读、未运行系统）。要点：

1. 架构：4个主Agent（Insight私有库挖掘/Media多模态搜索/Query新闻搜索/Report报告生成）+ 主持人LLM + 关键词优化小模型 + MindSpider爬虫；三个研究引擎代码约90%互为拷贝，无任何Agent框架，纯手写。
2. 单Agent是固定流水线而非ReAct：大纲5-6段 → 逐段"搜索→总结→反思×2-3（固定跑满，不评估增益、不提前终止）"→ 汇编Markdown；JSON约束靠"Schema嵌入prompt"，未用function calling，手写修复代码兜底。
3. "论坛"机制真相：`logs/forum.log`文本文件 + 后台线程每秒正则抓取三引擎stdout日志当"发言"（400+行日志解析/JSON修复代码）；每攒5条触发Qwen主持人总结；各Agent仅读"最新一条HOST发言"注入prompt——互相看不到原文，无消息总线、无调度器。
4. 终止条件：单Agent由段落数×反思数自然终止；论坛由"日志变短"或"约2小时无活动"结束；ReportEngine用"报告目录.md文件数超基线"这种文件计数信号判断三引擎完工。
5. 中文社媒数据靠自建爬虫、先爬后查：MindSpider每日从newsnow热榜取词 → DeepSeek提关键词 → MediaCrawler子模块（需登录态，灰色）爬微博/小红书/抖音/快手/B站/知乎/贴吧入MySQL/PG；InsightEngine只查库，无库则该Agent报废，无降级模式。
6. 省钱设计可直接抄：关键词口语化改写用小Qwen、情感分析用本地HF模型（tabularisai/multilingual-sentiment-analysis）、大结果集用MiniLM+KMeans聚类采样压到50条再喂LLM。
7. 最有价值的提示词资产：InsightEngine的"接地气搜索词"工程——禁书面语、分平台语言风格库（微博热搜词/B站弹幕/知乎问答体）+ 网络情感词汇库（`InsightEngine/prompts/prompts.py` 170-267行）。
8. ReportEngine是全项目最强模块：模板选择→LLM布局设计→字数预算→逐章生成Schema约束的JSON IR（17种块类型）→确定性渲染单文件交互式HTML（Chart.js全内联、离线可用）+ WeasyPrint PDF；四类失败分类重试+稀疏版兜底+SSE逐章流式。
9. 报告可视化是交互式Chart.js而非静态图；但图表数值由LLM转写产生、只校验结构不校验真伪，且无原始帖子级证据回链——数据溯源是明确可超越点。
10. LLM接入：7个角色各自独立KEY/BASE_URL/MODEL三元组（pydantic-settings+.env，前端可改），任何OpenAI兼容端点可用；官方推荐异构组合Kimi-K2/Gemini-2.5-Pro/DeepSeek/Qwen-plus。
11. 工程观感：Flask主控+3个Streamlit子进程iframe拼UI+5936行单文件index.html；`sys.path.append`满天飞、SECRET_KEY硬编码、疑似死代码；13个单测全是工具性测试，零报告质量评估。
12. 给新项目的空档：真消息总线代替日志抓取、智能终止+token预算、图表数据强制来自真实统计管道+证据链接、C端纯开放数据降级模式、并发章节生成、以及BettaFish完全没有的"历史洞察"能力（事件对比/周期/相似案例）。
13. 不确定处：MediaCrawler子模块本地为空未能核实内部实现；个别死代码判断基于静态矛盾推断。
