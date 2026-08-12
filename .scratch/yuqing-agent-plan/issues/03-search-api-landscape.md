# 03 搜索API全景调研

Type: research
Status: resolved

## Question

2026年8月当前，适合本项目（中文舆情、社媒/新闻搜索、小额预算）的搜索/内容获取API全景：每家给出免费层额度、付费价格、中文搜索质量、社媒与新闻覆盖、时效性、API易用性、key申请路径。至少覆盖：博查Bocha、智谱搜索、Tavily、Serper、Brave Search、Exa、Jina、Firecrawl、SearXNG自建，以及其他值得关注的国内外选项。产出：选型对比表 + 推荐组合（主力+兜底）+ 用户配置文档的素材（PRD要求给用户写一份搜索API文档）。

调研成果写入：`.scratch/yuqing-agent-plan/research/03-search-api-landscape.md`

## Answer

完整报告：`../research/03-search-api-landscape.md`（含对比表、逐家六维度详情、推荐组合、key申请步骤；关键数字于2026-08-12经官网核实，无法核实处已标注）。要点：

1. 智谱 Web Search 是中文舆情性价比之王：search_std 0.01元/次、search_pro 0.03、search_pro_sogou 0.05（覆盖腾讯新闻+知乎）；自带时间过滤（oneDay~oneYear）、域名过滤、发布时间字段、count 1-50——舆情参数最全。无免费层但按次计费无月费。
2. 博查Bocha中文质量公认第一梯队：DeepSeek官方搜索引擎；AI Search含抖音/头条/微博内容；约0.02元/次、新用户1000次试用（价格页需登录，接入前复核）。其国际线 LangSearch 个人完全免费、无需信用卡，带freshness+长摘要，可做零付费默认主力（QPS上限未公开）。
3. 百度千帆 web_search 每月免费1500次 + 最多100站点白名单过滤（限时免费）；AI搜索V2另有100次/天限免。
4. 秘塔API 0.03元/次、送5000点，自带网页全文获取接口，可兼做原文抓取。
5. Tavily 免费1000 credits/月无需绑卡，PAYG $0.008/credit；Agent生态集成最好，但中文结果偏英文/境外来源——只当境外/英文补充。2026-02被Nebius收购，价格未变。
6. Serper 免费2500次（一次性），付费$1/千次起、credits 6个月过期；最便宜的Google News中文时效兜底。
7. Brave不推荐：2026-02-12起免费层取消，必须绑卡+每月$5额度，中文覆盖弱。
8. 原文抓取：Jina免key可用（约20 RPM）、新key送1000万token，但大陆直连不通（需代理）；Firecrawl免费1000页/月（官网当日文案为每月）且开源可自部署；建议再内置httpx+trafilatura零依赖兜底。
9. SearXNG自建内置baidu/sogou/sogou wechat（微信公众号搜索）/quark/360等中文引擎，零边际成本，但需海外VPS无SLA——作可选高级配置，不作C端默认。
10. 推荐组合——零付费默认：LangSearch（主力）+ 百度千帆（补充）+ Tavily（境外兜底）+ Jina免key/Firecrawl（原文）；质量优先：智谱search_pro_sogou或博查（主力）+ Tavily/Serper（境外）+ 秘塔reading或Jina（原文）。调用顺序按"有付费key用付费主力，否则走免费链逐级降级"。
11. 残留不确定项：博查官网现行价格、百度超额单价、LangSearch QPS上限、Firecrawl免费层"每月vs一次性"存第三方矛盾说法。
