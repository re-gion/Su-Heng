# 舆情Agent规划地图

Label: wayfinder:map

## Destination

一份"开工级方案包"，另存于原PRD（`舆情agent开发-初步PRD.md`，只读）之外：成熟版SPEC（产品定位、功能规格、创新点、专报内容规格）+ 系统架构设计（多智能体框架与通信、Loop终止条件、数据流、技术栈、工程结构）+ 分阶段实施路线图。判定标准：拿着方案包就能直接开工写代码，没有悬而未决的关键决策。编码实现是下一个 effort。

## Notes

- 竞赛为契机但时间宽裕，以做出真正好用的C端舆情专报产品为主
- 小额预算：搜索API/数据源免费层优先、可小额付费买额度；LLM低成本优先（如deepseek），支持自主配置多模型
- 产品形态已定：开源自部署 + 演示站（非在线SaaS运营）
- 原始PRD只读，不修改；成熟SPEC另存
- 参考代码库（本地）：Pi `D:\a311\系统赛\全智赛\全智赛-OPC-设计与业务参考\pi-main\pi-main`；BettaFish `D:\a311\系统赛\全智赛\全智赛-OPC-设计与业务参考\BettaFish-main\BettaFish-main`
- 用户是入门开发者：方案要架构清晰、易维护、学习成本可控
- 决策票用 /grilling + /domain-modeling；样张票用 /prototype；调研票由 /research 子agent解决，调研成果存 `.scratch/yuqing-agent-plan/research/`（本仓库尚无提交，不用 throwaway 分支）
- 舆情分析的底线：数据真实、引用可溯、不编造；所有设计决策都要过这条线

## Decisions so far

<!-- 每关一张票加一行：[票名](issues/NN-slug.md) — 一句话结论 -->

- [01 BettaFish深度剖析](issues/01-bettafish-analysis.md) — 无框架纯手写：固定流水线（非ReAct）、"论坛"实为日志文件正则抓取、终止靠文件计数、社媒数据全押自建爬虫且无降级；最强模块是ReportEngine（JSON IR→交互式HTML）；可超越点：真消息总线、智能终止、图表数据真实管道+证据回链、纯开放数据降级、历史洞察能力。
- [02 Pi设计哲学提炼](issues/02-pi-design-philosophy.md) — Pi=严格单向分层+极小内核+扩展/数据文件承载能力；可搬用：分层强制、SSE事件流、markdown定义子Agent、skills渐进披露、工具契约与拦截钩子；不学：动态加载用户代码、无权限系统。
- [03 搜索API全景调研](issues/03-search-api-landscape.md) — 零付费默认组合=LangSearch(免费)+百度千帆(月免1500)+Tavily(境外)+Jina/Firecrawl(原文)；质量优先=智谱search_pro_sogou或博查为主力；Brave已无免费层不推荐；策略为"付费主力优先、免费链逐级降级"。
- [05 多智能体框架选型调研](issues/05-agent-framework-selection.md) — 倾向轻量自研（asyncio+AsyncOpenAI工厂+FastAPI SSE，核心编排估200-400行），备选LangGraph 1.x；多LLM全走OpenAI兼容base_url一个工厂覆盖；CrewAI/AutoGen/MetaGPT/两家官方SDK均排除；预设了改投LangGraph的触发条件。
- [07 历史洞察Agent的取舍与替代路径](issues/07-history-agent-decision.md) — 保留Agent，放弃提前爬虫囤库；数据层重构为三层合规轻数据：公开数据集冷启动+热榜快照本地积累(SQLite)+搜索API按需回溯；社媒评论采集降为"用户自带Cookie、默认关闭"可选插件，三层独立降级。
- [08 创新点与差异化定位](issues/08-differentiation-innovation.md) — 第一卖点=可核验性（claim级引用+自动核验+三级标记+证据留存）；支撑点=结构化专报三件套（时间线/传播/历史对照）、零门槛体验、异构降幻觉与合规轻数据的技术叙事；区隔=BettaFish重数据不可溯源 vs 本项目轻数据全合规可核验。
- [09 多智能体架构与通信机制设计](issues/09-multi-agent-architecture.md) — 轻量自研（asyncio+AsyncOpenAI工厂+FastAPI SSE，三纪律保迁移）；论坛=进程内ForumBoard黑板+SSE可视化；小Loop终止=增益自评+硬上限，大Loop放行=主持人评审无关键未解决项或轮数/预算上限（强制放行须写局限性声明）；每轮state落盘SQLite解决卡死续跑；报告Agent只吃结构化证据库+摘要+决议。
- [10 幻觉控制与引用可核验机制](issues/10-hallucination-citation-design.md) — 证据先行：搜索结果先入证据库（快照存档），claim必须绑定evidence_id杜绝编造来源；LLM异构交叉核验→三级标记（已证实≥2独立信源/待核验/争议）；信源分级；无引用claim渲染层拒入正文；验收指标=引用覆盖率+核验通过率。
- [11 舆情专报样张原型](issues/11-report-mockup.md) — 样张定稿（海天事件，`prototypes/report-mockup-v1.html`）：十板块规格+新闻编辑室视觉确立；评审决议：核验徽章升级四级制（增"已证伪"）、官方级单源可判已证实、传播图表改证据库真实口径+热榜叠加、编辑判断接受但硬规则隔离、报告IR支持速览/完整双版。
- [13 MVP范围与分期路线图](issues/13-mvp-roadmap.md) — 三步走：M0骨架竖切（单agent+证据库+四级核验+速览报告端到端）→V1参赛MVP（三agent论坛+完整专报+过程可视化+配置页，历史洞察按需回溯）→V1.5（数据集+热榜积累+PDF+演示站）→V2（Cookie插件/NLI）；每阶段有可演示交付物与量化验收。
- [12 技术栈与工程结构确认](issues/12-tech-stack-structure.md) — 前端改为Vite+React SPA（弃Next.js），FastAPI静态托管、单Python进程部署；单仓分层（学Pi单向依赖+数据文件定义agent）；LLM按角色三元组配置、开箱即跑；SearchProvider适配器+配额降级链；统一SSE事件协议；存储单SQLite零外部依赖。
- [04 国内社媒数据获取现实调研](issues/04-social-data-access.md) — 官方API对个人基本关死；爬虫要登录态且有判例级法律风险（微博诉蚁坊判赔500万与本场景同款）；"提前爬1个月历史库"不建议；合规替代=DailyHotApi(MIT)热榜+新闻聚合API+公开数据集可覆盖约60-70%需求，深度评论只能做"用户自带Cookie、默认关闭"的可选插件。
- [06 竞品与学术扫描](issues/06-competitor-landscape.md) — "C端空白"部分成立（相邻形态已存在）；BettaFish 4.2万star证明需求、其issues给出四大痛点（无溯源/幻觉/卡死/部署难）；站得住的增量=claim级可核验引用+结构化专报+中文特化；同构多agent辩论降幻觉证据不足，应走异构分工+证据仲裁。
- [14 成熟SPEC撰写定稿](issues/14-final-spec.md) — 开工级方案包已定稿于 `docs/方案包/`（README+产品SPEC+架构设计+路线图+搜索配置指南，2112行）；6项票面矛盾/缺口已显式处理并经用户确认；补充硬要求=核验器LLM与各agent同等独立配置。**本地图到达目的地（2026-08-12）。** 同日经 Codex 对抗性审查修订：5项P0/12项P1落实（双ID体系、已证伪判定对象、证据状态机、当事方规则、event_log、预算表、额度/判例/许可纠错、内容治理等），新增第6份文档 `05-核心契约.md`；详见票14 Amendment 与方案包 README §七。

## Not yet specified

（地图收官：原雾区各项均已在方案包中落位——或已成为正式设计（敏感话题降级、缓存/去重/存证、历史数据层、双版IR、多LLM配置、演示站部署计划），或显式标注为"实现期决定"（图表库选型、PDF技术路径等9项，见 `docs/方案包/README.md` 汇总）。演示用标杆事件已有候选：海天样张事件。）

## Out of scope

- 舆情监测与预警（B端功能，PRD明确为日后扩展方向）
- 商业舆情数据库采购与接入
- 在线SaaS运营（已定开源自部署+演示站）
- 编码实现本身（本地图只到"开工级方案包"，实现是下一个 effort）
