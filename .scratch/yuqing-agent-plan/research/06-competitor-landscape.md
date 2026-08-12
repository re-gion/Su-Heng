# 竞品与学术调研：C端舆情专报Agent 的市场空白验证与差异化机会

- 调研日期：2026-08-12
- 调研方式：网络搜索 + GitHub API + HN Algolia API。所有论断附来源 URL；无法找到证据的判断标注「未验证」或「推断」。
- 核心待验证假设：**「市面上没有面向C端个人用户的舆情服务」**

---

## 0. 结论摘要（TL;DR）

**「C端舆情完全空白」这一假设不成立，但「窄口径空白」成立。**

- 严格意义上的「舆情监测/分析产品」确实几乎全部面向 B端/G端（企业、政府），没有找到一个定位纯 C端、活着且好用的舆情产品——这部分假设**成立**。
- 但「个人用户想搞清楚一个舆情事件」这个需求，已经被多种**相邻形态**部分覆盖：国外的 Ground News / Particle（偏见对比+AI摘要）、国内的知微事见（公众可查的事件影响力/时间线库）、已死的「后续」App（新闻时间线）、腾讯较真AI（事实查证）、以及爆火的开源项目 BettaFish（自部署多Agent舆情分析，42k stars）——这部分假设**不成立**。
- 真正的空白是**交付形态**：输入任意事件 → 零部署 → 多agent自动调查 → 输出**带句子级引用、可点击核验、区分已证实/待核验/争议**的结构化 HTML 专报。目前没有任何产品完整做到这一点（BettaFish 最接近，但部署门槛高、报告被用户吐槽缺信息来源、幻觉率高）。

---

## 1. 面向个人用户的舆情/事件梳理产品盘点

### 1.1 国内：舆情产品几乎全是 B/G 端

主流产品定位均为企业/政府客户，按年付费 SaaS 或人工服务：

| 产品 | 定位 | 来源 |
|---|---|---|
| TOOM舆情 | 企业/政府，3万+站点监测，App仅是通知渠道 | https://www.toom.cn/ |
| 清博舆情 | 企业形象管理，7*24人工分析服务 | https://yuqing.gsdata.cn/ |
| 艾普思舆情 | SaaS模式，企业全网舆情态势 | https://www.ipscg.com/ |
| 识达/舆情秘书 | 企业品牌口碑监测 | https://m.cidastar.com/knows/20240312160437525 |
| 人民网舆情数据中心、识微商情等 | 政企报告服务 | https://blog.csdn.net/qq_43664361/article/details/142555290 |

行业共识也把舆情类产品归入 B端范畴（参考 C/B/G 端产品讨论：https://zhuanlan.zhihu.com/p/641213915 ）。**未找到任何一款定位「个人用户日常查舆情」的商业化产品**——搜索结论与假设一致。

### 1.2 国内：近C端的相邻形态（重点）

1. **知微事见（知微数据）**——最接近「C端舆情」的存活产品。面向公众开放的热点事件库：事件影响力指数、传播趋势、时间线（渠道参与时间轴）、媒体观点聚类、人群画像，公众可搜索事件、也可「提交分析」由人工处理。
   来源：https://pidoutv.com/sites/156.html 、https://research.zhiweidata.com/2021/01/14/46035a39f3/
   局限：只覆盖已收录的热点事件、以数据看板呈现而非调查式报告、定制分析需排队人工处理；深度付费面向机构（免费额度与付费边界**未验证**）。
2. **「后续」App**——纯C端新闻时间线产品的前车之鉴。机器抓取+人工筛选，把热点事件按时间线整理，信息源限于有采编资质的媒体和官方通报。2019年4月因「不可抗力」被中国区 App Store 下架；2021年时间线核心功能被削减；2022年8月下线、12月复活、2023年再次停更；目前仅 Telegram 频道仍在更新。
   来源：https://sspai.com/post/53915 、https://www.geekpark.net/news/232310 、https://houxu.app/p/10113 、https://www.163.com/dy/article/G1TU7DTP0545UIAV.html 、https://t.me/s/HouXuApp
   **关键教训：它死于新闻资质与监管，不是死于需求不存在。**
3. **腾讯较真AI**（2025年8月上线）——面向大众的 AI 事实查证工具：动态推理、实时追踪查证逻辑、权威机构+专家库+AI交叉验证、可信度评分。属于「单条传闻查证」，不做整事件专报。
   来源：https://view.inews.qq.com/a/20250819A0426T00?scene=qb_ranking
4. **公众号/小程序形态**：未发现以「个人事件时间线梳理/舆情专报」为定位的小程序；较真辟谣小程序（2018年起）和「全国网络辟谣」小程序属于辟谣查询，非事件调查（来源：https://cloud.tencent.com/developer/news/17550 ）。存在大量「吃瓜聚合」灰产站点，宣称按时间线归档爆料，但内容无核验、有法律风险，不构成正规竞品。**「小程序形态无正规竞品」基于本次搜索，属弱证据，标注：未充分验证。**
5. **壹伴等热点榜单工具**：面向自媒体运营者选题，非事件调查（来源：https://yiban.io/blog/24257 ）。

### 1.3 国外：C端「新闻透视」类产品已相当成熟

- **Ground News**（2018年起，iOS/Android/Web/浏览器插件）：聚合5万+信源，按左中右偏见、事实性、所有权标注，Bias Bar / Blindspot 对比同一事件的跨立场报道。是「C端媒体偏见对比」的成熟标杆；批评者（CJR）指出「聚合+贴标签」可能制造独立分析的假象。
  来源：https://ratingfacts.com/blogs/ground-news-review-take-a-look-at-this-bias-busting-news-app 、https://factually.co/fact-checks/media/ground-news-comparison-60a467
- **Particle**（$10.9M A轮，Lightspeed）：AI多源摘要+聊天机器人+音频简报+政治光谱视图，2026年多篇评测将其列为最佳AI新闻聚合器。
  来源：https://www.readless.app/blog/best-ai-news-aggregators-2026 、https://wisp.news/blog/ground-news-alternatives/
- **AllSides / NewsGuard**：左中右对照、信源可信度评级（浏览器插件）。来源：https://factually.co/product-reviews/electronics-tech/best-news-aggregator-apps-spotting-media-bias-2026-85e251
- **Timeline News（Android）**、**Newslines/Chronologies**（协作时间线）等小众时间线产品存在但影响力有限。来源：https://play.google.com/store/apps/details?id=com.soumyazyx.timelinenews&hl=en 、https://alternativeto.net/software/newslines/
- Google Alerts / Mention 等属于关键词提醒/品牌监测，不是事件调查（本次未深查，标注：未验证细节）。

**小结**：国外 C端「理解新闻事件」市场已拥挤（偏见对比、AI摘要、可信度评级），但均以**英文新闻媒体**为语料，不覆盖中文社媒舆论场，也不产出调查式专报。

---

## 2. 事件时间线与事实核查：产品与研究现状

### 2.1 时间线生成（Timeline Summarization, TLS）

- **CHRONOS**（阿里通义实验室，NAACL 2025）：迭代自我提问 + RAG 做开放域新闻时间线总结，发布 Open-TLS 数据集（记者撰写的时间线做金标准）。已开源，**方法可直接借鉴，但无 C端产品化**。
  来源：https://arxiv.org/abs/2501.00888 、https://github.com/Alibaba-NLP/CHRONOS 、https://www.qbitai.com/2025/01/242019.html
- **Timeline Summarization in the Era of LLMs**（SIGIR 2024）：chunking / 知识图谱 / TimeRanker 三种路线。来源：https://dl.acm.org/doi/10.1145/3626772.3657899
- **NTS-CoT**：针对时间线生成的幻觉问题，Element-CoT + Causal-CoT，较 SOTA 提升 AR-1 23.4%、Date-F1 10%。来源：https://arxiv.org/pdf/2606.13171
- 产品侧：微软曾做 Bing Spotlight 时间轴新闻聚合（来源：https://www.geekpark.net/news/232310 ）；国内「后续」App 已死（见1.2）。**结论：学术方法成熟、中文 C端产品缺位。**

### 2.2 事实核查工具

- 国外人工核查：Snopes（NewsGuard 100/100 评级）、PolitiFact、FactCheck.org、Full Fact、Google Fact Check Explorer（聚合 ClaimReview 标记）。
  来源：https://www.snopes.com/ 、https://journaliststoolbox.ai/ai-fact-checking-tools/ 、https://rightblogger.com/blog/ai-fact-checking-tools
- 自动化辅助：ClaimBuster（识别值得核查的句子）、TinEye（图片溯源）、Deepware（深伪检测）、NewsGuard 插件（信源评级；其对 ChatGPT/Bard 的审计发现在热点新闻话题上有 80–98% 概率复述虚假叙事）。
  来源：https://www.successtechservices.com/ai-fact-checking-tools/ 、https://gulfnews.com/amp/gulfnews/technology/media/openai-chatgpt-google-bard-spreading-news-related-misinformation-report-1.1692165919697
- 国内：腾讯较真（2015年品牌，2018年小程序，2025年8月推出「较真AI」智能查证）、中国互联网联合辟谣平台（2018年上线，104家单位联动，网站+App+微信/支付宝小程序等10个终端，2026年更名「全国网络辟谣」并开设涉企辟谣专区）。
  来源：https://cloud.tencent.com/developer/news/17550 、https://view.inews.qq.com/a/20250819A0426T00?scene=qb_ranking 、https://zh.wikipedia.org/zh-hans/中国互联网联合辟谣平台 、https://www.piyao.org.cn/
- **结论：核查生态是「单条传闻→真/假判定」，没有人把核查状态（已证实/待核验/争议）嵌入到整事件的专报叙事里——这是可占位的呈现形态。**

---

## 3. 通用 AI Deep Research 产品：用户拿它查舆情事件能得到什么？

### 3.1 国外三家现状

| 产品 | 强项 | 弱项 | 来源 |
|---|---|---|---|
| OpenAI Deep Research | 最深，5–30分钟长报告 | 贵；仍会幻觉 | https://www.helicone.ai/blog/openai-deep-research |
| Perplexity Deep Research | 最快（<3分钟），免费5次/天，SimpleQA 93.9% | 深度有限 | https://glasp.co/articles/deep-research-tools-compared |
| Gemini Deep Research | 便宜、展示研究计划 | 受SEO偏差影响，引用最难追溯 | https://aiixx.ai/blog/ai-deep-research-tools-compared-gemini-openai-and-perplexity |

共同缺陷（多篇横评一致）：都会幻觉、难判断信源可信度；**幻觉更多表现为「错引真实来源」而非编造来源**，用户必须逐条点开引用核验。来源：https://www.punku.ai/blog/comprehensive-analysis-deep-research-implementations 、https://www.aryabhconsulting.com/blog/deep-research-ai-tools-comparison-2025-gemini-vs-chatgpt-vs-perplexity-for-business-research

### 3.2 国内

- Kimi 探索版：多信息源广搜、数据详尽；秘塔：引用溯源好、百科式聚合；知乎直答：社区语料+5000万篇学术论文、知识库；豆包深度搜索：边想边搜补信息缺口。
  来源：https://blog.csdn.net/weixin_40774379/article/details/143880697 、https://www.zhihu.com/question/646387052 、https://zhuanlan.zhihu.com/p/1894882216909206731 、https://zhuanlan.zhihu.com/p/26587895144

### 3.3 本项目相对 Deep Research 的真实增量必须是什么

用户拿 Deep Research 查舆情事件，能得到：一份基于**公开网页/新闻**的综述报告 + 文末引用列表。得不到的（前3条有来源支撑，后3条为合理推断，标注**推断**）：

1. **中文社媒舆论场数据**：微博/小红书/抖音/快手的评论区民意——Deep Research 走搜索引擎，抓不到需要登录/反爬的社媒评论（BettaFish 为此专门自建爬虫集群，反证了搜索引擎路径拿不到；来源：https://github.com/666ghj/BettaFish ）。
2. **可核验性保障**：横评一致结论是引用需人工逐条核验、错引真实来源常见（见3.1）——没有一家做「引用自动核验+核验状态展示」。
3. **时间线与传播结构**：Deep Research 输出自由文本综述，不做结构化时间线、传播节点、情绪量化（知微事见做后者但不做调查报告）。
4. **推断**：不做「已证实/待核验/争议」的事实分层；一段传闻和一条官方通报在报告里权重无区分。
5. **推断**：不做历史相似事件对照（如「此类事件历史上如何演化/反转」）。
6. **推断**：无面向分享的标准化专报交付物（可保存、可转发、可复查的 HTML 工件）。

**站得住的增量 = 中文社媒民意 + claim级可核验引用 + 时间线/传播/历史对照的结构化专报。只做「搜索+总结」会被大厂 Deep Research 直接碾压。**

---

## 4. BettaFish（微舆）深度分析

### 4.1 公开数据与讨论度

- GitHub（2026-08-12 经 GitHub API 实测）：**41,989 stars / 7,630 forks / 累计 442 个 issues（7 open）**，GPL-2.0，仓库创建于 2024-07，最近推送 2026-08-10，维护活跃。https://github.com/666ghj/BettaFish
- 曾连续两天 GitHub 热榜第一（来源：https://zhuanlan.zhihu.com/p/1969452034932580571 、https://www.ctocio.com/best/41574.html ）；知乎有多篇部署教程与源码剖析（https://zhuanlan.zhihu.com/p/2000169303241143897 、https://zhuanlan.zhihu.com/p/1972715382730142918 ）。
- **海外几乎零讨论**：HN 上该项目仅 1 分 0 评论（2025-11-03，经 HN Algolia API 实测）。热度基本局限于中文社区。
- 衍生生态：BettaFish-skill（Claude Code/Cursor 的 Skill 封装版，宣称零配置、WebSearch 实时取数）https://github.com/XiaoMaColtAI/BettaFish-skill ——说明社区自己都在想办法绕开原版的部署门槛。

### 4.2 架构

Flask 并行调度多 Agent：Insight Agent（自建舆情库挖掘）+ Media Agent（多模态社媒内容）+ Query Agent（中外网页搜索），ForumEngine 协同研判，自建爬虫集群覆盖微博/小红书/抖音/快手等10+平台。来源：https://zhuanlan.zhihu.com/p/1972715382730142918 、https://github.com/666ghj/BettaFish

### 4.3 已知问题（来自 issues 实测抽样 + 社区反馈）

1. **幻觉与倾向**：issue「幻觉率太高且带有负面批判性倾向」。
2. **报告可核验性缺失**：issue「生成的报告能否获取到信息来源」——报告不带引用溯源。
3. **稳定性**：issue「运行了三个小时……在反思中不明不白睡死了，没法rerun没法终止没法跳过生成报告」；时间戳缺失导致反思逻辑故障。
4. **部署门槛高**：需多个 AI 平台 API key、强制 MySQL、Docker 403 等部署类 issue 多；知乎教程称其「功能强但部署较复杂」（https://zhuanlan.zhihu.com/p/1992530888559445194 ）。
5. **内容审查中断**：敏感词触发模型安全审查导致整个分析流程跑不通（官方讨论区反馈，v1.2.0 讨论：https://github.com/666ghj/BettaFish/discussions/227 ）。
6. **其他**：爬虫效率低、国外事件分析弱、页面报错、等待时间长。

（issue 标题来自 GitHub Search API 实测：repo:666ghj/BettaFish type:issue，2026-08-12）

**含义：BettaFish 用 42k stars 证明了「个人级舆情分析」需求真实存在且巨大；同时它的部署门槛、幻觉、无引用、跑一次三小时，正好圈出了 C端产品要解决的四个痛点。**

---

## 5. 学术侧可借鉴方法（挑重点）

### 5.1 多Agent协作/辩论降低幻觉

- 奠基工作：Du et al.《Improving Factuality and Reasoning in LMs through Multiagent Debate》（ICML 2024），多实例多轮辩论提升事实性。https://arxiv.org/abs/2305.14325
- **必须知道的反面证据**：《If Multi-Agent Debate is the Answer, What is the Question?》系统评测发现 MAD 在9个基准上**不能稳定优于** CoT/Self-Consistency 等单Agent基线；但**模型异构（不同底模混编）能显著改善 MAD**。https://arxiv.org/html/2502.08788v1
- 从众效应：弱模型在辩论中仅纠正 3.6% 的立场偏差，倾向放弃正确判断随大流。https://arxiv.org/pdf/2606.10296
- Khan et al. 2024：当辩手掌握裁判没有的信息时，辩论能提升裁判准确率——适合「各Agent各自掌握不同信源」的舆情调查场景。https://arxiv.org/abs/2402.06782
- **工程启示：别迷信同构辩论；用「不同底模+不同信源分工+证据仲裁」的异构结构，并保留单Agent+自一致性作为对照基线。**

### 5.2 LLM引用生成与可验证性（attributed QA）

- **ALCE** 基准（Gao et al. 2023）：引用质量 = citation recall（被引文档能否蕴含生成句）+ precision（去掉某文档后是否仍蕴含），用 NLI 模型自动判定；当时最佳模型在 ELI5 上 50% 的句子缺乏完整引用支持。https://arxiv.org/abs/2305.14627
- AIS/AutoAIS 框架（Rashkin et al.；Bohnet et al. 2022 attributed QA）：把「可归因于已识别来源」形式化为 NLI 任务。前置系统：WebGPT、GopherCite。来源见 ALCE 相关工作：https://www.emergentmind.com/papers/2305.14627
- 近期可直接抄的工程件：VeriCite（RAG 引用严格核验，https://arxiv.org/pdf/2510.11394 ）、细粒度引用定位（https://arxiv.org/pdf/2408.04568 ）、引用粒度研究（https://arxiv.org/pdf/2604.01432 ）。
- **工程启示：专报的每个 claim 走「生成→NLI核验→不通过则重检索或降级为『待核验』」管线，并把 citation recall/precision 做成可对外展示的质量指标——这正是 Deep Research 和 BettaFish 都没有的。**

### 5.3 舆情传播分析/民意模拟

- 时间线方法见 2.1（CHRONOS 可直接复用）。
- S³：LLM Agent 社交网络模拟，复现信息/态度/情绪传播（性别歧视场景 Acc 66.2%）。https://arxiv.org/abs/2307.14984
- Chuang et al.：LLM Agent 有「趋真偏差」，模拟民意分化需显式注入确认偏差。https://arxiv.org/abs/2311.09618
- FDE-LLM：动力学方程+LLM Agent 融合，区分意见领袖/跟随者，优于传统 ABM。https://www.nature.com/articles/s41598-025-99704-3
- 综述：LLM社会模拟 survey https://arxiv.org/pdf/2412.03563
- **工程启示：V1 别做「预测舆情走向」（BettaFish 宣称做，学术上准确率仅60%+且有系统性偏差）；先做「已发生传播的结构化描述+历史相似事件对照」，这更可验证。**

---

## 6. 差异化机会点清单

### 6.1 已拥挤（别正面打）

- B/G端舆情监测 SaaS（TOOM/清博/识微/艾普思…）
- 通用 Deep Research（OpenAI/Perplexity/Gemini/Kimi/秘塔/知乎直答）——「搜索+长文综述」无差异化空间
- 英文世界的偏见对比/新闻聚合（Ground News/Particle/AllSides）
- 单条传闻辟谣（较真AI、全国网络辟谣、Snopes 系）

### 6.2 真空白（有证据支撑）

1. **零部署的 C端「事件→专报」交付形态**：BettaFish 42k stars 证明需求，442 个 issues 里大量部署/稳定性抱怨证明现有供给把 C端用户挡在门外。
2. **claim级可核验引用**：Deep Research 横评公认「错引真实来源」是最大坑，BettaFish 被用户直接要求加信息来源；ALCE/VeriCite 提供了现成的核验方法论，但没人产品化到舆情场景。
3. **中文事件时间线自动生成**：「后续」App 死于资质不是死于需求，CHRONOS 方法开源可用，无中文 C端产品在做。
4. **事实分层呈现**（已证实/待核验/争议，随事件进展更新）：核查生态只做单条判定，专报生态（BettaFish）不做核查状态。
5. **历史相似事件对照洞察**：知微事见有「同类事件对比」数据但无叙事分析；Deep Research 不主动做（推断）。

### 6.3 本项目最有希望的立足点（3–5个）

1. **「可核验」作为第一卖点**：每个论断带可点击引用 + NLI 自动核验状态 + 已证实/待核验/争议三级标记，对外公布 citation recall/precision 指标。直接回应 Deep Research 与 BettaFish 的共同软肋，且有 ALCE 系方法可落地、可量化自证。
2. **零门槛 C端体验**：网页/对话输入事件名即出报告，对比 BettaFish 的「多API key+MySQL+Docker+三小时」，把开源验证过的需求接到普通人手里。
3. **专报的结构化三件套：时间线 + 传播分析 + 历史对照**——这是通用 Deep Research 自由文本综述给不了的信息架构；时间线用 CHRONOS 式迭代自问，传播分析先做描述性（声量/节点/情绪），不做走向预测。
4. **异构多Agent交叉验证**：不同底模+不同信源分工（新闻检索/社媒/官方通报/历史库）+ 证据仲裁Agent，规避同构辩论从众失效的坑（arXiv:2502.08788），把「降幻觉」做成可讲的技术故事。
5. **合规路线**：不自建社媒爬虫、不做新闻分发（「后续」App 之死 + BettaFish 爬虫的灰色地带都是前车之鉴），走「检索公开信源+引用原文链接+个人研究工具」定位，降低资质与监管风险。

### 6.4 主要风险（如实列出）

- **大厂覆盖风险**：Deep Research 产品迭代极快，若仅做「搜索+报告」会被顺手覆盖；护城河必须落在核验管线、专报信息架构与中文社媒特化上。
- **数据获取瓶颈**：不自建爬虫则微博/小红书评论区民意拿不全——「民意量化」深度将弱于 BettaFish，需要在产品叙事上以「可核验的事实调查」而非「全量民意监控」定位（这是取舍，不是缺陷）。
- **内容安全**：舆情事件天然涉敏，BettaFish 已出现敏感词导致流程中断的问题；需要设计降级策略而非硬闯。
- **「C端舆情付费意愿」未验证**：本次调研证明了需求存在（BettaFish热度、后续App口碑），但没有找到 C端为舆情类产品付费的直接证据，商业化假设仍需 MVP 验证。
