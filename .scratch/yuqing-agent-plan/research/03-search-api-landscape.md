# 搜索/内容获取 API 全景调研（面向中文舆情专报 Agent）

- 调研日期：2026-08-12
- 调研方法：官网/官方文档直接抓取（curl，当日）+ 网络搜索 + 社区实测文章引用。所有关键数字带来源 URL 与查证日期；查不到的明确标注"未能核实"。
- 项目前提：面向 C 端的中文舆情专报 Agent，开源自部署、用户自带 key（BYOK）、小额预算（免费层优先，可少量付费）。核心诉求：中文新闻/社媒检索质量、时效性（最近几天）、新闻域过滤、时间范围过滤、原文抓取。

---

## TL;DR

1. **中文舆情主力**首选国产 API：智谱 Web Search（0.01–0.05 元/次、带搜狗引擎覆盖腾讯新闻+知乎、时间/域名过滤齐全）与博查 Bocha（中文质量公认第一梯队、社媒模态覆盖抖音/头条/微博）。
2. **免费额度池**够开发和轻量使用：LangSearch（博查旗下，个人免费）、百度千帆 web_search（1500 次/月）、Tavily（1000 credits/月）、Serper（2500 次一次性）、Jina（1000 万 token）、Firecrawl（1000 credits/月）。
3. **海外三家（Tavily/Exa/Brave）中文社媒覆盖弱**，只适合做境外中文媒体/英文信息的补充；Brave 2026 年 2 月已砍掉免费层且必须绑卡，不推荐。
4. **Jina Reader 大陆直连不通**（2026-04 国内服务器实测超时），自部署在国内服务器时必须有代理或换方案。

---

## (a) 选型对比表

| # | 服务 | 免费层（查证 2026-08-12） | 付费价格 | 中文/社媒覆盖 | 时效性/时间过滤 | 大陆直连 | 舆情适配要点 |
|---|------|--------------------------|----------|---------------|----------------|----------|--------------|
| 1 | **博查 Bocha** | 新用户可领 1000 次试用资源包（活动性质，官网价格页需登录，未能在页面直接核实长期政策） | 约 0.02 元/次，资源包低至 3.6 元/千次 | ★★★★★ 国内第一梯队；AI Search 含抖音/头条/微博等模态 | freshness 参数（oneDay 等） | ✅ 稳定 | 新闻/社媒垂类、长摘要 summary、国内合规 |
| 2 | **LangSearch**（博查免费国际线） | 个人与小团队免费、无需信用卡；QPS 上限未公开（未能核实） | 免费产品，无公开付费档 | ★★★★ 同博查索引体系 | freshness: oneDay/oneWeek/oneMonth/oneYear | ✅（api.langsearch.com） | 免费 + Bing 兼容格式 + summary 长文摘要 |
| 3 | **智谱 GLM Web Search** | 无明确免费搜索次数（新人代金券活动不定期，未能核实）；GLM Coding Plan Lite 含 100 次/月搜索 MCP | search_std 0.01 元/次；search_pro 0.03；搜狗/夸克引擎 0.05 | ★★★★★ 搜狗引擎覆盖腾讯生态+知乎 | search_recency_filter（oneDay~oneYear）+ 返回网页发布时间 | ✅ | 时间过滤+域名过滤+count 1–50+发布时间字段，舆情参数最全 |
| 4 | **秘塔 Metaso** | 免费 5000 点（≈5000 次，上线活动数据） | 0.03 元/次 | ★★★★ 自建数百亿中文索引，学术强 | 支持全网/学术等 scope（时间过滤粒度未能核实） | ✅ | 搜索+网页全文读取+问答三接口，自带原文获取 |
| 5 | **百度千帆 AI 搜索** | web_search 1500 次/月（按天发放）；AI 搜索 V2 100 次/天限免 | 超额按量后付费（单价见官方计费页，未能核实具体数字） | ★★★★ 百度索引，百家号/贴吧等百度生态 | 实时信息；站点过滤（100 站点，限时免费） | ✅ | 指定站点搜索=天然新闻域过滤；免费额度稳定 |
| 6 | **Tavily** | 1000 credits/月，无需信用卡（basic 搜索 1 credit/次） | PAYG $0.008/credit；套餐 $30/月起；advanced 搜索 2 credits | ★★ 中文结果多为境外/英文来源，微博知乎覆盖差 | topic=news、time_range/days 参数 | ✅ 偶有波动 | Agent 生态集成最好；include/exclude_domains；Extract API |
| 7 | **Serper** | 2500 次一次性，无需信用卡 | $50/5 万次（$1/千次）起，量大至 $0.30/千次；credits 6 个月过期 | ★★★ Google 中文结果；境外中文媒体好，墙内社媒差 | Google News endpoint + tbs=qdr 时间过滤 | ✅ 服务可达 | 最便宜的 Google News 来源；gl/hl 参数定向中文 |
| 8 | **Brave Search API** | 免费层已于 2026-02-12 取消；现为绑卡后 $5/月免费额度（约 1000 次） | Search $5/千次；Answers $4/千次+$5/M tokens | ★ 自建英文索引，中文弱 | freshness 参数、news endpoint | ✅ | 不推荐：必须绑外币卡、中文覆盖差 |
| 9 | **Exa** | 注册送 $20（约 2800 次）+ 每月 $10 免费额度 | Search $7/千次（含前 10 条正文）；contents $1/千页；deep $12–15/千次 | ★★ 语义搜索强但中文索引不全 | category=news、startPublishedDate/endPublishedDate | ✅（Cloudflare） | 语义找"相似报道"独有；中文舆情主力不合适 |
| 10 | **Jina (s.jina.ai / r.jina.ai)** | 无 key 免费（约 20 RPM）；新 key 送 1000 万 token | 按 token 计费，Reader 约 $0.02/M 输出 token；free key 100 RPM | ★★★ 读取中文页面质量好；s.jina.ai 搜索一般 | r.jina.ai 实时抓取任意 URL | ❌ 2026-04 实测大陆直连超时 | 原文抓取首选（需海外网络）；转 Markdown 供 LLM |
| 11 | **Firecrawl** | 1000 credits/月（≈1000 页/月），无需绑卡 | Hobby $19/月起（年付约 $16）；Standard/Growth 更高（第三方数据） | ★★★ 抓取通用网页可，反爬站点需付费 stealth | scrape 实时抓取；search 2 credits/10 结果 | ✅ | 网页→Markdown；开源 AGPL 可自部署（无云端反爬层） |
| 12 | **SearXNG 自建** | 完全免费（VPS 成本约 $6/月） | 无 | ★★★★ 内置 baidu/sogou/sogou wechat/quark/360search/bilibili 等中文引擎 | time_range 参数（依引擎支持） | 自建实例国内可访问；聚合 Google/Bing 需海外 VPS | 零边际成本；sogou wechat=微信公众号搜索入口；无 SLA、有上游封锁风险 |
| 13 | 阿里云 IQS（参考） | 试用：个人 1000 次/15 天 | 约 34–42 元/千次（阶梯价） | ★★★★ 国内合规 | 实时联网搜索 | ✅ | 太贵，小预算不适合 |
| 14 | SerpAPI（参考） | 100 次/月 | $50/月 5000 次起（≈$9–25/千次） | ★★★ 支持 Baidu 引擎结构化结果 | 各引擎时间过滤 | ✅ | 免费层鸡肋、价格高，仅当需要百度 SERP 结构化时考虑 |

---

## 逐家详情

### 1. 博查 Bocha（open.bochaai.com）

- **免费层**：官网购买资源包页面可领免费试用 1000 次资源包；另有口令"博查搜索"兑换 1000 次的活动（活动性质，长期政策以控制台为准）。来源：[知乎·免费API资源包+高效搜索](https://zhuanlan.zhihu.com/p/1949503510795233209)（查证 2026-08-12）。官网 [open.bochaai.com](https://open.bochaai.com/) 价格页为 JS 渲染需登录，未能直接抓取核实——**接入前请以控制台实际显示为准**。
- **付费价格**：约 0.02 元/次（"不到微软必应同规格服务的三分之一"），API 资源包低至 3.6 元/千次。来源：同上知乎文；[阿里云云市场·博查搜索API](https://market.aliyun.com/detail/cmapi00069848)（提供 100 次/1000 次/100 万次套餐档）。
- **中文质量与社媒覆盖**：自称近百亿网页索引、DeepSeek 官方搜索引擎、腾讯/字节/阿里官方推荐、日调用 3000 万+（[官网](https://open.bochaai.com/)）。AI Search API 在全网搜索基础上增加抖音、头条、西瓜、微博等内容并返回天气/百科等模态卡（[掘金对比文](https://juejin.cn/post/7426765897325559834)）。2026-04 国内服务器实测：直连稳定、延迟低、中文搜索质量好，被作者选为主力搜索（[博客园·国内能用的搜索API实测对比, 2026-04-23](https://www.cnblogs.com/itech/p/19918043)）。
- **时效性**：Web Search API 支持 `freshness` 参数（oneDay/oneWeek/oneMonth/oneYear/noLimit），可搜到当天内容。
- **API 形态**：`POST https://api.bochaai.com/v1/web-search`，Bing Search 兼容风格 JSON，返回 snippet + 长文 `summary`；有官方 MCP server（[BochaAI/bocha-search-mcp](https://github.com/BochaAI/bocha-search-mcp)）。
- **舆情适配**：freshness 时间过滤、长摘要减少二次抓取、新闻/社媒垂类模态、国内合规（内容安全过滤）。**弱点**：免费额度是一次性活动包，长期使用要充值。

### 2. LangSearch（langsearch.com，博查旗下免费线）

- **免费层**："For individuals and small teams, we offer free access"、"Free, No credit card required"。来源：[langsearch.com 首页](https://langsearch.com/)、[GitHub README](https://github.com/langsearch-ai/langsearch)（查证 2026-08-12）。具体 QPS/日限额官方文档未公开——**未能核实**，社区反馈轻量使用无压力。
- **付费价格**：无公开付费档（免费产品）。
- **中文质量**：与博查同源索引体系，Bing 兼容响应格式。
- **时效性**：`freshness` 参数：oneDay/oneWeek/oneMonth/oneYear/noLimit（[官方 API 文档](https://docs.langsearch.com/api/web-search-api)）。
- **API 形态**：`POST https://api.langsearch.com/v1/web-search`，count 1–10，`summary=true` 返回长文摘要；另有免费 Rerank API。
- **舆情适配**：零成本主力候选；count 上限 10 条偏小，需多轮查询补量。

### 3. 智谱 GLM Web Search（bigmodel.cn）

- **免费层**：搜索 API 本身无常设免费次数（新用户注册送代金券/token 的活动不定期，**未能核实**当前是否覆盖搜索）。GLM Coding Plan Lite 订阅含联网搜索 MCP 100 次/月（[套餐概览](https://docs.bigmodel.cn/cn/coding-plan/overview)）。
- **付费价格**（官方文档当日抓取，查证 2026-08-12，来源：[联网搜索-智谱AI开放文档](https://docs.bigmodel.cn/cn/guide/tools/web-search)）：
  - `search_std`（自研基础版）：**0.01 元/次**
  - `search_pro`（自研高级版，多引擎协作降空结果率）：**0.03 元/次**
  - `search_pro_sogou`（搜狗：覆盖腾讯生态新闻/企鹅号 + 知乎）：**0.05 元/次**
  - `search_pro_quark`（夸克垂直内容）：**0.05 元/次**
- **中文质量与社媒覆盖**：搜狗引擎是关键差异点——微信生态（腾讯新闻/企鹅号）与知乎内容对舆情场景价值高；官方文档明确标注该覆盖范围。
- **时效性**：`search_recency_filter`（oneDay/oneWeek/oneMonth/oneYear/noLimit）；响应含网页发布时间字段，官方文档称"便于时效性分析和排序"。
- **API 形态**：REST + 官方 SDK（`pip install zai-sdk`），参数含 count（1–50）、search_domain_filter（域名过滤）、content_size（摘要长度）；另有 Search Agent API 和官方 MCP endpoint。
- **舆情适配**：参数最全（时间+域名+发布时间+50 条上限），按次计费无月费门槛，且很多目标用户已有智谱 key（BYOK 友好）。**弱点**：无免费层。

### 4. 秘塔 Metaso（metaso.cn）

- **免费层**：上线时赠送 5000 点免费额度。来源：[OSCHINA, 2025-06](https://www.oschina.net/news/362265)、[搜狐报道](https://www.sohu.com/a/917129776_211762)（查证 2026-08-12；为上线时数据，现行政策以 [metaso.cn/search-api](https://metaso.cn/search-api) 控制台为准）。
- **付费价格**：**0.03 元/次**（同上来源；官方称比 Bing 便宜约 70%）。
- **中文质量**：自建数百亿多语言索引，秘塔 AI 搜索 C 端产品日千万级调用验证；学术/文库垂类突出。
- **时效性**：支持全网/学术等 scope；时间范围过滤粒度**未能核实**（官网 API 页为 JS 渲染）。
- **API 形态**：搜索 API + **网页全文获取（reading）API** + 问答 API，metaso.cn 首页点 "API" 即可测试。
- **舆情适配**：自带网页全文获取接口（原文抓取可少接一家）；多模态（网页/图片/视频/文库）。

### 5. 百度智能云千帆 AI 搜索

- **免费层**（查证 2026-08-12，官方文档）：
  - 百度搜索 `web_search`：**每月免费 1500 次（按天发放）**，支持按量后付费，默认优先抵扣免费资源（[API 文档](https://cloud.baidu.com/doc/qianfan-api/s/Wmbq4z7e5)）
  - 百度 AI 搜索 V2（搜索+大模型总结）：**限时免费，每天 100 次**（[API 文档](https://cloud.baidu.com/doc/qianfan-api/s/em82g4tlk)）
- **付费价格**：超额按量后付费，具体单价在[千帆价格文档](https://cloud.baidu.com/doc/qianfan-docs/s/Jm8r1826a)，本次未能抓到明确数字——**未能核实**，接入前查计费页。
- **中文质量**：百度全网索引 + 百家号/贴吧等百度系内容；时效好（全网实时信息）。
- **时效性**：实时检索；返回摘要+网址。
- **API 形态**：`POST https://qianfan.baidubce.com/v2/ai_search/web_search`，API Key 鉴权，提供 API/工具/MCP 三种用法。
- **舆情适配**：**指定站点搜索（最多 100 个站点，付费功能限时免费）**——可直接实现"只搜权威新闻源"的白名单过滤；每月 1500 次免费额度稳定可预期。

### 6. Tavily（tavily.com）

- **免费层**：**1000 API credits/月，无需信用卡**（"1,000 free API credits per month with no credit card required"，[官方 pricing 页](https://www.tavily.com/pricing)当日抓取，查证 2026-08-12）。credits 每月 1 日重置。
- **付费价格**：PAYG **$0.008/credit**；套餐 $30/月起（官方页当日抓取）。计费：basic 搜索 1 credit、advanced 搜索 2 credits（[官方 credits 文档](https://docs.tavily.com/documentation/api-credits)当日抓取）。2026-02 被 Nebius 收购（约 $2.75 亿），价格体系未变（[ColdIQ](https://coldiq.com/blog/tavily-pricing)、[UsagePricing](https://www.usagepricing.com/blueprint/tavily)）。
- **中文质量**：中文社区实践反馈检索结果多为英文/境外来源，需二次翻译（[知乎实践文](https://zhuanlan.zhihu.com/p/16183565341)）；微博/知乎等墙内社媒覆盖差。2026-04 国内服务器实测：可直连但偶有波动，建议加重试（[博客园实测](https://www.cnblogs.com/itech/p/19918043)）。
- **时效性**：`topic="news"` + `days`/`time_range` 参数，可搜最近一天。
- **API 形态**：REST，LangChain/LlamaIndex 等框架集成最成熟；附带 Extract/Crawl API。
- **舆情适配**：include_domains/exclude_domains 域过滤、news 主题、Extract 原文抓取一体；适合做**境外中文媒体与国际视角**的补充信源。

### 7. Serper（serper.dev）

- **免费层**：**2500 次免费查询，一次性（非每月），无需信用卡**（[serper.dev 首页](https://serper.dev/)当日抓取"2,500 free queries...No credit card required"，查证 2026-08-12；一次性属性见 [ColdIQ 2026-07](https://coldiq.com/blog/serper-pricing)）。
- **付费价格**：预充值包 $50/5 万次（$1/千次）起，量大至 $0.30/千次；**credits 6 个月过期**；请求 >10 条结果计 2 credits（[ColdIQ](https://coldiq.com/blog/serper-pricing)、[ApiSerpent](https://apiserpent.com/blog/serper-pricing-credits-explained)）。
- **中文质量**：即 Google 搜索结果 API 化——Google 的中文新闻索引（境外中文媒体、大陆媒体的可收录部分）质量好，但墙内社媒（微博/微信）覆盖差。
- **时效性**：Google News endpoint + `tbs=qdr:d/w/m` 时间过滤，时效极好。
- **API 形态**：极简 REST（`google.serper.dev/search|news`），支持 `gl=cn&hl=zh-cn`；大陆直连服务可达（[博客园实测](https://www.cnblogs.com/itech/p/19918043)）。
- **舆情适配**：最便宜的 Google News 数据源；只给链接+摘要，需配原文抓取。

### 8. Brave Search API（brave.com/search/api）

- **免费层**：**原 2000 次/月免费层已于 2026-02-12 取消**。现为：所有计划须绑信用卡，含 $5/月免费额度（约 1000 次搜索）（[官方页](https://brave.com/search/api/)当日抓取"$5 in free monthly credits"+绑卡 FAQ，查证 2026-08-12；变更报道见 [AgentDeals](https://agentdeals.dev/vendor/brave-search-api)、[Scavio](https://scavio.dev/blog/brave-search-api-killed-free-tier-what-now-2026)）。
- **付费价格**：Search **$5/千次**（50 QPS）；Answers $4/千次 + $5/M tokens（官方页当日抓取）。
- **中文质量**：独立自建索引，以英文互联网为主，中文覆盖明显弱于 Google 系。
- **时效性**：freshness 参数、news endpoint。
- **API 形态**：REST，简单。
- **舆情适配**：**不推荐**——必须绑外币卡（对 C 端 BYOK 用户门槛高）、中文弱、免费层实质取消。

### 9. Exa（exa.ai）

- **免费层**：注册送 **$20 免费额度（约 2800 次搜索）+ 免费档每月再送 $10**（[官方 pricing 文档](https://exa.ai/docs/reference/pricing)当日抓取，页面更新于 2026-08-07，查证 2026-08-12）；另有无鉴权 MCP 免费档（150 次/天）。
- **付费价格**（官方页当日抓取）：Search **$7/千次**（2026-03 起含前 10 条结果正文）；contents $1/千页；deep search $12–15/千次；answer $5/千次。
- **中文质量**：语义/神经搜索是特色，但中文索引不全，国内对比文章评价"更适合国外，国内基本用不了（内容角度）"（[B站专栏对比](https://www.bilibili.com/read/cv36692629/)、[博客园实测](https://www.cnblogs.com/itech/p/19918043)——网络上可达）。
- **时效性**：`category=news` + `startPublishedDate/endPublishedDate` 精确日期过滤 + livecrawl 选项。
- **API 形态**：REST + SDK，参数丰富但学习成本高。
- **舆情适配**："找相似报道/相似观点"这类语义任务独一档；免费额度慷慨；但不适合做中文舆情主力。

### 10. Jina（s.jina.ai / r.jina.ai）

- **免费层**：不带 key 直接用 `https://r.jina.ai/<URL>` 免费（约 20 RPM）；**新 API key 送 1000 万 token**（[jina.ai 官网](https://jina.ai/reader/)当日抓取"ten millions tokens"，查证 2026-08-12）。带 key 免费档 100 RPM / 10 万 TPM。
- **付费价格**：按 token 购买，Reader 折合约 **$0.02/M 输出 token**（[ColdIQ](https://coldiq.com/tools/jina-ai)、[MakerStack](https://makerstack.co/reviews/jina-reader-review/)；官网充值页为 JS 渲染未直接核实单包价格）。
- **中文质量**：r.jina.ai 读取中文页面转 Markdown 质量好；s.jina.ai 搜索为抓取 SERP+读取前 5 结果，中文检索一般。
- **时效性**：r.jina.ai 实时抓取，无缓存陈旧问题。
- **API 形态**：URL 前缀即用，零学习成本；同一 key 通用于 Jina 全系 API。2025-10 被 Elastic 收购，长期独立性存疑（[ColdIQ](https://coldiq.com/tools/jina-ai)）。
- **关键风险**：**大陆直连完全不通**（2026-04-23 阿里云国内服务器实测连接超时，[博客园](https://www.cnblogs.com/itech/p/19918043)）。国内自部署用户需代理，或改用秘塔 reading/Firecrawl。
- **舆情适配**：原文抓取兜底首选（海外网络环境下），1000 万免费 token 按普通文章 2–5k token 算可读几千篇。

### 11. Firecrawl（firecrawl.dev）

- **免费层**：**1000 credits/月（≈1000 页/月），无需绑卡**（[官方 pricing 页](https://www.firecrawl.dev/pricing)当日抓取"scrape 1,000 pages every month (1,000 free credits per month)"，查证 2026-08-12）。注意有第三方称 2026-05 起改为一次性——与官网当日文案矛盾，**以官网为准（当前官网明确写 per month）**。
- **付费价格**：官网页当日见 Hobby **$19/月**；更高档 Standard/Growth 年付约 $83/$333/月（第三方 [eesel](https://www.eesel.ai/blog/firecrawl-pricing)、[affinco](https://affinco.com/firecrawl-pricing/)，未在官网直接核实）。scrape 1 credit/页；search 2 credits/10 结果；stealth 反爬模式 5 credits/页。
- **中文质量**：通用网页抓取转 Markdown 可靠；强反爬站点（微信文章等）成功率一般且需 stealth 加价。
- **时效性**：实时抓取；另有 search endpoint（搜索+抓取一体）。
- **API 形态**：REST + SDK；**开源 AGPL 可 Docker 自部署**（自部署缺云端反爬/代理层）。大陆直连可达（[博客园实测](https://www.cnblogs.com/itech/p/19918043)）。
- **舆情适配**：搜索命中后的原文抓取环节；自部署版可做零成本抓取兜底。

### 12. SearXNG 自建

- **成本**：软件免费开源；约 $6/月 VPS 即可运行（约 200MB 内存）（[dasroot 部署指南 2026-03](https://dasroot.net/posts/2026/03/self-hosted-search-searxng-installation-configuration/)）。
- **中文引擎**：官方 `settings.yml` 内置 **baidu、sogou、sogou wechat（微信公众号文章）、quark、360search、bilibili** 等中文引擎（[searxng/searxng master settings.yml](https://github.com/searxng/searxng/blob/master/searx/settings.yml)，查证 2026-08-12）——`sogou wechat` 对舆情场景是稀缺入口。
- **部署要点**：聚合 Google/Bing 需海外 VPS（国内 IP 请求不到上游）；JSON API 需在 settings.yml 的 `formats` 加 `json` 并关闭 `limiter`（[官方 Search API 文档](https://docs.searxng.org/dev/search_api.html)、[CSDN 教程](https://blog.csdn.net/qq_33906319/article/details/161106984)）。
- **时效性**：`time_range` 参数（day/week/month/year，依引擎支持）。
- **风险**：无 SLA；上游引擎改版/封 IP 会导致部分结果降级；无内容摘要（只有 snippet），需配原文抓取。
- **舆情适配**：零边际成本、无限量；作为免费兜底聚合层非常合适，但对 C 端 BYOK 产品而言"要求用户自建 SearXNG"门槛偏高，更适合作为**可选高级配置**。

### 13. 其他值得关注（简评）

- **阿里云 IQS 信息查询服务**：国内合规联网搜索；试用个人 1000 次/15 天；正式计费约 34–42 元/千次阶梯价（[计费说明](https://help.aliyun.com/zh/document_detail/2862023.html)，查证 2026-08-12）。**太贵，小预算不适合**，仅企业预算下考虑。
- **SerpAPI**：免费 100 次/月，$50/月 5000 次起（≈$9–25/千次），支持 Baidu/Google/Bing 多引擎结构化 SERP（[apiserpent 2026-07 对比](https://apiserpent.com/blog/serp-api-pricing-comparison)、[博客园实测](https://www.cnblogs.com/itech/p/19918043)）。免费层鸡肋，只在需要百度 SERP 结构化数据时考虑。
- **DuckDuckGo（ddgs 库白嫖）**：完全免费但非官方、不稳定，且**大陆直连不通**（[博客园实测](https://www.cnblogs.com/itech/p/19918043)），不建议依赖。
- **Bing Search API**：已于 2025 年关停，出局。
- **火山引擎/腾讯云**：搜索能力主要绑定各自大模型/Agent 平台，独立 Web Search API 对 BYOK 场景不友好，本次不展开。

---

## (b) 推荐组合（免费层优先、质量优先调用）

设计原则：用户自带 key → 配置越少越好；每个环节给"免费默认 + 付费更优"两档；国内直连必须可用。

### 主力中文检索（按优先级降序调用）

| 优先级 | 服务 | 触发条件 | 理由 |
|--------|------|----------|------|
| P0 | 智谱 Web Search（search_pro_sogou / search_std） | 用户配了智谱 key（很多人已有） | 0.01–0.05 元/次几乎无感；搜狗引擎覆盖腾讯新闻+知乎；时间/域名过滤+发布时间字段是舆情刚需；单 key 同时解决 LLM+搜索 |
| P0' | 博查 Bocha | 用户配了博查 key 且愿付费 | 中文质量公认最好、社媒模态最全；与智谱二选一作主力 |
| P1 | LangSearch | 免费默认（用户只需注册免费 key） | 博查同源、免费、带 freshness+summary；作为"零付费模式"的默认主力 |
| P2 | 百度千帆 web_search | 免费补充 | 1500 次/月免费+站点白名单过滤，百度系内容补盲 |

### 兜底/境外补充

| 优先级 | 服务 | 角色 |
|--------|------|------|
| P1 | Tavily（免费 1000 credits/月） | 境外中文媒体+英文信源补充；主力失败时兜底 |
| P2 | Serper（2500 次一次性） | Google News 中文时效兜底；用完即弃或按需充 $50 |
| P3 | SearXNG（用户自建，可选高级配置） | 无限免费聚合 + sogou wechat 公众号搜索 |
| 不推荐 | Brave | 绑卡门槛+中文弱+免费层已取消 |
| 特殊场景 | Exa | 仅"找相似报道/跨语言观察"任务；免费 $20+$10/月够用 |

### 原文抓取（搜索命中后取全文）

| 优先级 | 服务 | 条件 |
|--------|------|------|
| P0 | 直接用搜索 API 的长摘要（博查/LangSearch summary、智谱 content_size=high） | 大多数舆情摘要场景够用，省一次抓取 |
| P1 | Jina r.jina.ai（免 key 20 RPM 或 free key 1000 万 token） | 部署环境可出海时的首选 |
| P1' | 秘塔 reading API（0.03 元/次体系内） | 国内直连场景 |
| P2 | Firecrawl（免费 1000 页/月） | 需要更干净 Markdown/批量抓取时 |
| P3 | 自实现 httpx+trafilatura 兜底 | 开源自部署产品应内置的零依赖兜底（额外建议） |

**默认零付费配置**：LangSearch（主力）+ 百度千帆（补充）+ Tavily（境外兜底）+ Jina 免 key/Firecrawl 免费层（原文）。
**质量优先配置**：智谱 search_pro_sogou 或博查（主力）+ Tavily/Serper（境外）+ 秘塔 reading 或 Jina（原文）。

---

## (c) Key 申请步骤简述（用户配置文档素材）

| 服务 | 申请路径 | 步骤 | 门槛 |
|------|----------|------|------|
| 博查 | [open.bochaai.com](https://open.bochaai.com/) | 注册（手机号）→ 控制台创建 API KEY → 购买/领取资源包（可试口令"博查搜索"兑换 1000 次） | 手机号；长期用需充值 |
| LangSearch | [langsearch.com/dashboard](https://langsearch.com/dashboard) | 注册 → API Key Management 生成 key | 邮箱即可，免费 |
| 智谱 | [bigmodel.cn/apikey/platform](https://bigmodel.cn/apikey/platform) | 注册（手机号+实名）→ API Keys 页创建 → 充值少量余额（搜索按次扣费） | 手机号+实名认证 |
| 秘塔 | [metaso.cn/search-api](https://metaso.cn/search-api) | 登录秘塔账号 → 首页点 "API" → 控制台生成 key（送 5000 点） | 手机号 |
| 百度千帆 | [cloud.baidu.com（千帆控制台）](https://cloud.baidu.com/product-s/qianfan_home) | 注册百度智能云（实名）→ 千帆控制台开通"AI 搜索"→ 创建 API Key | 手机号+实名认证 |
| Tavily | [app.tavily.com](https://app.tavily.com/) | 邮箱/Google 注册 → 自动生成 key（tvly- 前缀） | 邮箱；国内访问官网可能需代理 |
| Serper | [serper.dev](https://serper.dev/) | 邮箱注册 → Dashboard → API Key（送 2500 次） | 邮箱 |
| Exa | [dashboard.exa.ai](https://dashboard.exa.ai/) | 邮箱注册 → API Keys（送 $20+每月 $10） | 邮箱 |
| Jina | [jina.ai](https://jina.ai/) | 免 key 可直接用 r.jina.ai；注册领 key（送 1000 万 token） | 邮箱；**国内直连不通需代理** |
| Firecrawl | [firecrawl.dev](https://www.firecrawl.dev/) | 邮箱/GitHub 注册 → API Keys（免费 1000 credits/月） | 邮箱 |
| Brave | [api-dashboard.search.brave.com](https://api-dashboard.search.brave.com/) | 注册 → **必须绑信用卡** → 每月 $5 免费额度 | 外币信用卡（不推荐） |
| SearXNG | 自建 | 海外 VPS + Docker 部署 → settings.yml 开 json 格式、关 limiter → 填自己实例 URL | 需 VPS 与动手能力 |

---

## 残留不确定项

1. 博查官网现行免费额度与单价未能从页面直接抓取（JS 渲染+需登录），以上为社区/市场渠道数据，接入前需登录控制台核实。
2. 百度千帆 web_search 超额单价未查到明确数字，需在计费页核实。
3. LangSearch 免费层 QPS/总量上限官方未公开，重度调用前需实测。
4. Firecrawl 免费层"每月 vs 一次性"存在第三方矛盾说法，官网当日文案为每月，建议接入后观察。
5. 秘塔 5000 点免费额度为 2025-06 上线时政策，现行政策未再核实。

## 主要来源清单

- 智谱官方文档（当日抓取）：https://docs.bigmodel.cn/cn/guide/tools/web-search
- Tavily 官方定价（当日抓取）：https://www.tavily.com/pricing ；credits 规则 https://docs.tavily.com/documentation/api-credits
- Exa 官方定价（当日抓取，页面更新 2026-08-07）：https://exa.ai/docs/reference/pricing
- Brave 官方（当日抓取）：https://brave.com/search/api/ ；免费层取消报道 https://agentdeals.dev/vendor/brave-search-api
- Serper 官网（当日抓取）：https://serper.dev/ ；定价解读 https://coldiq.com/blog/serper-pricing
- Firecrawl 官方定价（当日抓取）：https://www.firecrawl.dev/pricing
- Jina 官网（当日抓取）：https://jina.ai/reader/
- LangSearch：https://langsearch.com/ ；https://docs.langsearch.com/api/web-search-api
- 博查：https://open.bochaai.com/ ；https://zhuanlan.zhihu.com/p/1949503510795233209 ；https://juejin.cn/post/7426765897325559834
- 秘塔：https://www.oschina.net/news/362265 ；https://www.sohu.com/a/917129776_211762
- 百度千帆：https://cloud.baidu.com/doc/qianfan-api/s/Wmbq4z7e5 ；https://cloud.baidu.com/doc/qianfan-api/s/em82g4tlk
- SearXNG：https://docs.searxng.org/dev/search_api.html ；https://github.com/searxng/searxng/blob/master/searx/settings.yml
- 国内连通性实测（2026-04-23，阿里云国内服务器）：https://www.cnblogs.com/itech/p/19918043
- 阿里云 IQS 计费：https://help.aliyun.com/zh/document_detail/2862023.html
