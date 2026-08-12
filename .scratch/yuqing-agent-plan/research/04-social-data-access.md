# 社交媒体舆情数据获取与合规调研

> 调研时间：2026-08-12 ｜ 面向对象：面向 C 端、开源自部署的舆情专报 Agent（入门开发者）
> 覆盖平台：微博、抖音、小红书、快手、知乎、B站、新闻媒体
> 说明：本文所有 GitHub 数据均通过 GitHub API 于 2026-08-12 实测；法律与平台政策部分标注了来源 URL，查不到的地方已明说。

---

## 0. 一句话结论

对个人开发者来说，**官方 API 基本拿不到舆情所需的数据权限**（搜索、评论、传播链路几乎全被收进企业付费/合作通道）；**爬虫技术上可行但工程和法律成本都高**（强反爬、需登录态、账号风险、且舆情类抓取已有"抓公开数据也被判赔 500 万"的判例）；**最稳、最合规、性价比最高的路径是"热榜聚合 API + RSS + 新闻聚合 API + 公开数据集"**。关于"提前爬 1 个月历史库"的计划：**不建议作为开源工具的默认能力，现实性低、收益有限、法律与维护风险集中**，详见第 6 节。

---

## 1. 各平台官方 API / 开放平台现状（逐平台）

总体规律：2023 年之后，各大平台的"数据类接口"（搜索、评论、榜单、传播数据）几乎都改为**要求企业资质认证 + 合规场景审核 + 付费**，个人开发者普遍只能拿到"登录授权 + 自己账号的基础信息 + 内容发布"这类权限，拿不到"读别人内容做舆情分析"的权限。

### 1.1 微博
- 个人可注册开发者、完成实名、创建应用（一个账号最多管理 10 个应用），但**核心的 Search API 明确"仅对新浪合作开发者开放"**，需联系 @微博开放平台 单独申请，个人走不通。来源：[Search API 官方 wiki](https://open.weibo.com/wiki/Search_API)
- 未审核应用只给 15 个测试账号，授权也只适用于开发者本人账号；支付等能力只向"公司开发者"开放。来源：[微博开放平台](https://open.weibo.com/)、[非应用开发者如何获得 App Key](https://docs.pingcode.com/ask/ask-ask/209408.html)
- 舆情/批量数据已被引导到**面向企业的"商业数据 API"付费通道**（定位就是"数据分析、舆情监控"，付费独享资源 + 关键词订阅推送）。来源：[微博 API 指南（幂简集成）](https://www.explinks.com/blog/sina-api-series-weibo-api-explores-the-value-of-social-data-to-help-creators-and-developers-innovate-1/)
- **个人开发者结论：拿不到舆情数据权限。** 能拿到的只有 OAuth 登录 + 自己账号的读写。

### 1.2 抖音（open.douyin.com）
- 提供授权登录、用户信息、视频管理、评论互动、数据分析等接口，但**自 2024 年起所有涉及数据采集的 API 均要求企业资质认证，个人开发者无法获取高频调用权限**。来源：[抖音开放平台申请全流程（2025 版）CSDN](https://blog.csdn.net/api_open/article/details/149418660)、[抖音 API 权限申请（知乎）](https://zhuanlan.zhihu.com/p/2029510500954776118)
- 数据类接口审核看重"合规场景 + 明确数据用途 + 不存储隐私、不二次分发"。核心是"企业资质 + 合规场景"。
- **个人开发者结论：数据采集类权限基本无缘，需企业主体。**

### 1.3 小红书（open.xiaohongshu.com）
- 开放平台目前**以电商/服务商场景为主**，主要面向企业开发者（自研商家、三方 ISV），入驻要营业执照、类目审核，上架服务市场还要交保证金（线下银行转账、需同主体）。来源：[小红书开发者文档](https://open.xiaohongshu.com/document/developer/file/1)、[授权流程（软件服务商）](https://open.xiaohongshu.com/document/developer/file/38)、[申请与调用（阿里云）](https://developer.aliyun.com/article/1642615)
- 权限分"功能权限"和"数据权限"，个人在官方渠道空间极小。网上大量号称能申请"笔记详情/评论"接口的，多为**第三方非官方数据服务**，与官方开放平台不是一回事，合规风险自负。
- **个人开发者结论：官方拿不到内容/评论数据权限。**

### 1.4 快手（open.kuaishou.com / developers.kuaishou.com）
- 开放能力（登录、用户管理、内容分享、直播、电商、小程序）以 SDK/OpenAPI 形式提供，创建应用后默认给基础接口，更多接口需在后台按需申请审核。来源：[快手开放平台](https://open.kuaishou.com/)、[快手开发者社区](https://developers.kuaishou.com/)
- 开放重心在**电商、小程序、直播等企业业务场景**，个人可申请的"内容/评论数据类"权限有限，且有严格频率限制。
- **个人开发者结论：舆情数据权限有限，偏企业业务。**

### 1.5 知乎
- **没有活跃维护的、面向个人开发者的公开 API 平台。** 网上讲"知乎 API v4/OAuth"的多是第三方技术文章（PingCode、百度云等）转述内部 App 接口，非官方对外开放文档，稳定性和合规性都无保证。来源：[知乎 API v4 整理（百度云）](https://cloud.baidu.com/article/3879126)
- **个人开发者结论：无正规官方开放 API。**

### 1.6 B 站（openhome.bilibili.com / open.bilibili.com）
- 官方开放平台面向机构、UP 主、品牌服务商，**社区普遍反馈"不对个人开发者开放认证"**。想做"监控 UP 主更新"这类需求的个人，只能转向 GitHub 上的"野生"第三方 API 文档（携带 cookie 模拟请求），且有风控和封禁风险。来源：[B站开发者入驻文档](https://open.bilibili.com/doc/4/cbdcee3b-f57e-5c7b-cf27-83892fb811c4)、[个人开发者实践反馈](https://d.cellmean.com/p/b32e1e05bde4)
- **注意：** 支撑 B 站第三方开发的两大非官方项目已在 2026 年初被**归档（archived）**（见第 3 节），意味着社区维护在收缩。
- **个人开发者结论：官方认证走不通，靠野生 API + cookie，风险自负。**

### 1.7 新闻媒体
- 无"统一官方 API"，但有成熟的**第三方新闻聚合 API**（聚合数据、天行数据、探数数据等），见第 4 节。这是唯一对个人开发者友好、能直接买到"内容 + 部分舆情标签"的正规通道。

> **逐平台小结**：舆情最需要的三类数据——**搜索结果、评论、传播/热度数据**——在官方 API 层面，个人开发者六大平台**基本全部拿不到**。唯一现实的官方付费通道是微博商业数据 API（企业主体）和第三方新闻聚合 API。

---

## 2. 爬虫可行性与风控现状

技术上"能爬"，但六个平台反爬强度、登录态要求、账号风险差异很大。以下均引用社区/项目实际反馈。

| 平台 | 反爬强度 | 是否需登录态(Cookie) | 社区反馈的实际情况 |
|---|---|---|---|
| 微博 | 中 | 搜索/分组类需要 | m.weibo.cn 移动端相对好爬；但有地区限制（境外抓不到视频）、图片防盗链需改 URL 才能显示。来源：[RSSHub 社交媒体路由](https://rsshub-doc.pages.dev/social-media)、[微博 Cookie issue](https://github.com/DIYgod/RSSHub/issues/12731) |
| 小红书 | **极强（公认最严）** | 需要，且易失效 | 常见报错"当前笔记暂时无法浏览"，需排查滑块验证码、Cookie 过期、平台风控；MediaCrawler 也大量此类 issue。来源：[MediaCrawler issue #550](https://github.com/NanmiCoder/MediaCrawler/issues/550) |
| 抖音 | 强 | 需要 | RSSHub 官方标注"反爬严格，需启用 puppeteer"；视频 CDN 校验 Referer，内嵌播放常失败。来源：[RSSHub 社交媒体路由](https://rsshub-doc.pages.dev/social-media) |
| 快手 | 强 | 需要 | 有严格频率限制，超频触发限流/封禁 |
| 知乎 | 中偏强 | 需要（z_c0/d_c0/__zse_ck 等） | 风控升级频繁，配好 Cookie 仍可能突然失效（2025-10 有此类 issue）。来源：[RSSHub issue #20303](https://github.com/DIYgod/RSSHub/issues/20303) |
| B 站 | 中 | 需要 cookie | 直接 curl 会被风控拦；30 分钟内登录错误>5 次锁 1 小时；滥用 API 会被封（一般 1 小时自动解封，不影响官网）。来源：[bilibili API 第三方文档](https://qinshixixing.gitbooks.io/bilibiliapi/) |

**登录态与账号风险的普遍共识：**
- 舆情所需的搜索、评论、二级评论几乎都**需要登录态**（Cookie / 已登录浏览器上下文）。这意味着必须挂真实账号，而**高频抓取会累积账号异常/封号风险**——这是社区反复提到的现实（本文因工具安全策略未能逐一取到"封号案例"原文，故此处标注为**社区普遍反馈而非逐案实证**）。
- MediaCrawler 最新已默认改用 **CDP 模式**（连接用户自己已登录的 Chrome，复用登录态/Cookie/扩展）**以降低平台风控检测**——这本身就侧面说明"风控真实存在、纯自动化容易被识别"。来源：[MediaCrawler README](https://github.com/NanmiCoder/MediaCrawler)
- Cookie 会过期、平台风控持续升级，**爬虫脚本需要长期、持续维护**（RSSHub 大量国内路由因反爬失效即是明证）。

**给自部署用户带来的额外难题：** 开源工具让用户"自部署 + 自己填 Cookie"，等于把**账号风险、验证码、IP 代理、脚本失效**全部转嫁给不懂技术的 C 端用户，落地体验会很差。

---

## 3. 成熟开源采集项目现状（GitHub API 实测，2026-08-12）

| 项目 | Star | Fork | 最近提交 | 状态 | 平台/用途 | License |
|---|---|---|---|---|---|---|
| [NanmiCoder/MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) | **61.8k** | 12.1k | 2026-08-11 | **活跃，未下架** | 小红书/抖音/快手/B站/微博/贴吧/知乎 的内容+评论 | Other（非商业学习） |
| [sansan0/TrendRadar](https://github.com/sansan0/TrendRadar) | **61.4k** | 24.9k | 2026-07-17 | 活跃 | 多平台**热点/热榜**聚合+RSS+关键词筛选+推送（舆情监控） | GPL-3.0 |
| [666ghj/BettaFish（微舆）](https://github.com/666ghj/BettaFish) | **42.0k** | 7.6k | 2026-08-10 | 活跃 | 多 Agent 舆情分析助手，宣称覆盖微博/小红书/抖音/快手等 30+ 平台 | GPL-2.0 |
| [SocialSisterYi/bilibili-API-collect](https://github.com/SocialSisterYi/bilibili-API-collect) | 20.2k | 2.9k | 2026-01-30 | **已归档 (archived)** | B站非官方 API 文档 | - |
| [shengqiangzhang/examples-of-web-crawlers](https://github.com/shengqiangzhang/examples-of-web-crawlers) | 14.7k | 3.8k | 2025-06-28 | 偏教学，更新缓 | 各类爬虫示例 | MIT |
| [dataabc/weiboSpider](https://github.com/dataabc/weiboSpider) | 9.7k | 2.1k | 2026-02-04 | 活跃 | 微博用户数据爬虫 | 无 |
| [cv-cat/Spider_XHS](https://github.com/cv-cat/Spider_XHS) | 7.2k | 1.3k | 2026-07-29 | 活跃 | 小红书采集 | 无 |
| [Nemo2011/bilibili-api](https://github.com/Nemo2011/bilibili-api) | 4.2k | 645 | 2026-07-06 | **已归档 (archived)** | B站 Python 封装库 | - |
| [imsyy/DailyHotApi](https://github.com/imsyy/DailyHotApi) | 4.0k | 1.3k | 2026-03-11 | 活跃 | 热榜聚合 API（支持 RSS/Vercel） | MIT |
| [dataabc/weibo-search](https://github.com/dataabc/weibo-search) | 2.3k | 429 | 2026-06-05 | 活跃 | 微博关键词/话题搜索采集（Scrapy） | 无 |
| [javabloger/yuqing（思通舆情）](https://github.com/javabloger/yuqing) | 627 | - | **2023-01-04（停更）** | 疑似废弃 | 开源舆情系统 | - |
| [lxw15337674/weibo-trending-hot-history](https://github.com/lxw15337674/weibo-trending-hot-history) | 26 | 8 | 2026-08-12 | 活跃（每小时抓） | **微博热搜历史数据**（2024-05-20 起） | MIT |

**关键判断：**
- **MediaCrawler 没有"删库/下架"**——网传"删库"是 **2024 年初的旧闻**，之后恢复，现为该领域事实标准（同类第一）。但请注意两点风险：① **License 是"非商业学习使用"许可**，把它嵌进"开源发布给 C 端用户"的产品里，**商用/分发合规存疑**；② 作者的增强版 **MediaCrawlerPro（多账号、IP 代理、JS 签名服务）已转为独立/闭源商业产品**（本次在 GitHub 公开路径下已查不到对应仓库），说明"能稳定绕过风控的那部分能力"正在闭源化、商业化。来源：[MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)、[2024 删库旧闻](https://www.cnblogs.com/mq0036/p/18097598)
- **B 站生态在收缩**：两大非官方项目（API 文档 + Python 库）双双**归档**，后续 B 站接口变动将无人跟进。
- **值得直接参考/复用的两个舆情项目**：**TrendRadar**（热榜+RSS+推送，架构轻、合规友好，和"专报"定位高度契合）和 **BettaFish/微舆**（多 Agent 舆情分析，功能全但依赖 AI 爬虫集群、合规风险更高）。这两个都是 GPL，二次开发需注意开源义务。
- **微博热搜历史数据集**（lxw15337674）证明"热榜/热搜历史"这类数据**已经有人在持续公开积累**，可直接取用，无需自己重爬。

---

## 4. 轻量替代路径（对个人/开源项目最现实）

### 4.1 热榜/热搜聚合（覆盖"当下热点 + 传播热度"约 50-60% 的舆情需求）
- **[imsyy/DailyHotApi](https://github.com/imsyy/DailyHotApi)**（4k★，MIT，可自部署/Docker/Vercel，自带 RSS 模式）：一个接口聚合几十个平台热榜。**MIT 许可、可商用、可自部署**，最适合做开源产品底座。
- **[sansan0/TrendRadar](https://github.com/sansan0/TrendRadar)**（61k★，GPL-3.0）：热榜聚合 + 关键词筛选 + AI 简报 + 多渠道推送，几乎就是"舆情专报"的雏形，可直接参考架构。
- 现成在线服务：tophub.today、[HotList-Web](https://github.com/uxiaohan/HotList-Web) 提供免费聚合接口。
- **覆盖能力**：能拿到各平台热搜榜单、排名、热度值、话题；**拿不到**单条帖子的全部评论和精确传播链路。

### 4.2 RSS（覆盖"指定源持续追踪"，稳定性中等）
- **[RSSHub](https://github.com/DIYgod/RSSHub)**（45.7k★，仍高频维护）：能给微博、B站、知乎、豆瓣等生成 RSS。**但国内社交平台路由在公共实例上基本已被反爬打废**，必须**自建实例 + 配 Cookie（微博 WEIBO_COOKIES、B站 BILIBILI_COOKIE 等）+ 抖音开 puppeteer + 国内 IP**，且路由会随平台风控升级失效，需持续维护。来源：[RSSHub 社交媒体路由](https://rsshub-doc.pages.dev/social-media)
- **结论**：RSS 适合"新闻媒体、博客、部分公开栏目"，对强反爬社交平台**不稳定、不建议作为主数据源**。

### 4.3 公开数据集（覆盖"历史语料 + 模型训练"，一次性成本最低）
- 情感/舆情语料：[weibo_senti_100k](https://github.com/SophonPlus/ChineseNlpCorpus)（12 万条带情感标注）、[kuroneko5943/weibo16](https://huggingface.co/datasets/kuroneko5943/weibo16)（16 个舆情话题，Apache-2.0，适合话题级）。
- 热搜时间序列：[weibo-trending-hot-history](https://github.com/lxw15337674/weibo-trending-hot-history)（小时级，2024-05 起持续）、[微博热搜博物馆](https://github.com/interestingcn/weiboresoubowuguan)。
- 数据集索引：[CLUEDatasetSearch](https://github.com/CLUEbenchmark/CLUEDatasetSearch)。
- **覆盖能力**：非常适合做**Demo、模型微调、历史回溯的冷启动**；但**时效性差**（不是实时），不能替代实时舆情。

### 4.4 新闻聚合 API（覆盖"新闻媒体舆情"，对个人最友好的付费正规通道）
- **[聚合数据 juhe.cn](https://www.juhe.cn/docs/api/id/235)**：新闻头条（免费 50 次/天）、地区新闻、AI 新闻简报；**"新闻舆情"接口**（含情感属性、舆情标签、原文链接）是**企业付费**产品。
- **[天行数据 TianAPI](https://www.tianapi.com/)**：多类免费新闻接口，按会员等级给每日额度。
- **[探数数据](https://www.tanshuapi.com/news/detail-107)**：新用户送 1 万次。
- **覆盖能力**：新闻媒体维度舆情覆盖好，正规、稳定、可商用；社交平台 UGC（帖子/评论）覆盖不了。

> **替代路径综合覆盖度评估**：热榜聚合 API + 新闻聚合 API + 公开数据集，能覆盖"**热点发现、热度排名、新闻媒体舆情、历史语料**"约 **60-70%** 的核心舆情需求，且**全部合规、可商用、可自部署、维护成本低**。缺口是"社交平台单条内容的**海量评论与精确传播链路**"——而这恰恰是官方 API 拿不到、爬虫风险最高的部分。

---

## 5. 法律合规边界

### 5.1 适用法律框架
我国没有专门的"爬虫法"，规制散见于《数据安全法》《个人信息保护法》《反不正当竞争法》《网络安全法》《刑法》。来源：[数据爬虫合规边界（知乎）](https://zhuanlan.zhihu.com/p/474228255)、[德恒：抓取公开数据的行为边界](https://www.dehenglaw.com/CN/tansuocontent/0008/025285/7.aspx)

### 5.2 三条"红线"（触碰即高风险）
1. **拿个人信息 → 刑事风险（侵犯公民个人信息罪）。** 典型：**杭州魔蝎数据案**——利用爬虫长期保存用户账号密码、抓取个人信息，被认定侵犯公民个人信息罪。舆情场景里，抓取并存储用户昵称/头像/手机号/关系链等**可识别个人的数据**是最危险的动作。
2. **破解/绕过反爬、高频打垮服务器 → 破坏计算机信息系统罪。** 典型：**高频访问深圳居住证网站致其瘫痪**被判破坏计算机信息系统罪。破解验证码、绕过风控、无视频率控制都可能落入此罪。来源：[爬虫合法性与法律边界](https://blog.axiaoxin.com/post/data-crawler-compliance/)
3. **抓平台数据做"实质性替代" → 不正当竞争。** 即使抓的是公开数据、即使没违反 robots，只要对被抓平台构成实质性替代或干扰其正常运营，仍可能判赔（见下）。

### 5.3 "公开 ≠ 可随便抓"（核心概念）
- 华东政法高富平教授的判断框架：看①是否**开放数据**（"公开"不等于"开放"，公开数据仍是私人控制下的数据）②手段是否合法③目的是否合法④是否造成损害。来源：[德恒](https://www.dehenglaw.com/CN/tansuocontent/0008/025285/7.aspx)
- 新浪微博诉超级星饭团案："对公开数据，平台应在一定程度上容忍他人合法收集利用，否则有违互联互通精神"——**但这种容忍有限度**。

### 5.4 代表性判例（对"舆情工具"最有警示意义的排在最前）
| 案件 | 结果 | 对本项目的警示 |
|---|---|---|
| **微博诉蚁坊"鹰击"舆情系统案**（(2019)京73民终3789号） | 判赔 **500 万+28 万**，构成不正当竞争 | **最直接的对标判例**：一家"网络舆情监测工具提供商"，抓微博数据做舆情监测分析被重判。法院认定：抓取手段不正当、加重平台服务器负担、改变平台数据展示规则、削减平台数据商业化机会。来源：[鹰击案分析（知乎）](https://zhuanlan.zhihu.com/p/547560088) |
| **新浪微博诉脉脉案**（数据不正当竞争第一案） | 判赔 200 余万 | 确立"**三重授权**"（用户授权平台+平台授权第三方+用户授权第三方）原则，未经三重授权抓用户数据即违法。来源：[中国法学网](http://iolaw.cssn.cn/flxw/201605/t20160502_4640475.shtml) |
| **大众点评诉百度案** | 判赔 300 余万 | **即使未违反 robots**，对数据"过度使用/实质性替代"仍构成不正当竞争。来源：[案例解析（知乎）](https://zhuanlan.zhihu.com/p/114298103) |
| **抖音诉小葫芦案** | 构成不正当竞争 | 抓取平台**非公开数据**、整理后公开展示，破坏平台数据展示规则，判负。列北京知产法院涉数据反不正当竞争十大典型案例之首。来源：[北京知产法院典型案例](https://www.ipeconomy.cn/index.php/index/news/magazine_details/id/6915.html) |
| **微信群控软件案**（全国首例微信数据权益案） | 判赔 260 万 | 抓取账号数据、好友关系链构成不正当竞争 |

### 5.5 相对安全区（同时满足才安全）
参考各律所合规要点，"相对安全"= **合法数据源 + 合理技术手段 + 正当用途 + 不造成损害**，具体：
- 只抓**真正开放的公开数据**（如新闻、公开榜单），**不碰个人隐私信息、不抓关系链**；
- **不破解、不绕过反爬、不伪装绕过访问控制**；尊重 robots；
- **严格控制频率**（一种说法是不超过目标站日均流量的 1/3），不干扰对方正常运营；
- **不做实质性替代**（不把人家的内容原样搬来替代其服务）；
- 对已抓数据**及时脱敏、按需删除**；不二次分发个人信息。
来源：[网络爬虫数据合规（知乎）](https://zhuanlan.zhihu.com/p/671064577)、[最高检：爬取数据须遵规](https://www.spp.gov.cn/llyj/202202/t20220210_543998.shtml)

### 5.6 "开源发布工具"这一动作的额外风险
- **提供爬虫工具本身**若被用于侵犯个人信息/破坏系统，开发者可能承担帮助/连带责任；把"能绕过风控的采集能力"作为默认功能开源分发，风险高于自用。
- MediaCrawler 的"**非商业学习使用**"许可正是这种风险的体现——它明确不授权商用。你的开源 C 端产品若内置或依赖此类组件，**License 兼容性和商用边界要单独评估**。

---

## 6. 综合判断：对"提前爬 1 个月历史库"的现实性评估

### 6.1 现实性评估：**低，不建议作为默认方案**
| 维度 | 评估 |
|---|---|
| 技术可行性 | 中。单平台、短周期、低频、自用，MediaCrawler 类工具能爬到一些；但**6 平台 × 评论 × 传播数据 × 稳定跑 1 个月**，工程量大、脚本易挂、要处理验证码/Cookie/代理，入门开发者难以稳定维护。 |
| 数据质量 | 差到中。强反爬平台（小红书/抖音）采集残缺率高；评论/传播数据尤其难拿全。 |
| 法律风险 | **高且集中**。舆情监测抓社交数据正是**鹰击案 500 万判赔**的同款场景；若抓到并存储用户个人信息，叠加**刑事风险**。"提前囤库"意味着**大规模、系统性、长期存储**，恰恰是判例中被认定"加重平台负担、构成不正当竞争"的加重情节。 |
| 收益 | 低。历史舆情数据**时效性衰减快**，1 个月前的社交热点对"实时专报"价值有限；且公开数据集/热搜历史已能覆盖冷启动需求。 |
| 对开源分发的适配性 | 差。真正有价值的是"用户自己的实时数据"，而你囤的库无法随产品分发（分发他人个人数据违法），等于自己承担全部风险却给用户带不来对应价值。 |

**一句话：投入产出比和风险收益比都不划算——高风险、高维护、低时效、难分发。**

### 6.2 替代建议（按推荐度排序）
1. **冷启动用公开数据集**：用 weibo_senti_100k、weibo16、微博热搜历史数据集做 Demo、模型微调、历史回溯——**零采集风险、零维护**，1 天就能搭出可展示的原型。
2. **实时层用"热榜聚合 API + 新闻聚合 API"**：以 **DailyHotApi（MIT，可商用自部署）** 做热点/热度底座 + 聚合数据/天行新闻 API 做媒体舆情。合规、稳定、可随开源产品分发。直接参考 **TrendRadar** 架构。
3. **深度数据做成"用户自带 Cookie 的可选插件"**：若确实需要评论/深挖，把 MediaCrawler 类能力设计成**用户在自己环境、用自己账号、自担风险**的可选模块，产品默认关闭，并在文档中明确合规提示——把账号与法律风险留在用户侧、留在"个人自用/研究"的相对安全区，而**不是你预先囤库替所有人承担**。
4. **明确产品合规红线**：默认只碰公开数据与榜单、不抓个人身份信息与关系链、内置频率限制与 robots 尊重、不做实质性替代、不二次分发个人信息；开源仓库附合规声明与免责。
5. **若未来要做重度社交数据**：现实路径是**企业主体 + 官方付费商业 API（如微博商业数据 API）或采购持牌数据服务商**，而不是个人爬虫囤库。

---

## 7. 主要来源清单
- 平台官方：[微博开放平台](https://open.weibo.com/)、[微博 Search API wiki](https://open.weibo.com/wiki/Search_API)、[抖音开放平台流程 CSDN](https://blog.csdn.net/api_open/article/details/149418660)、[小红书开发者文档](https://open.xiaohongshu.com/document/developer/file/1)、[快手开放平台](https://open.kuaishou.com/)、[B站开发者入驻](https://open.bilibili.com/doc/4/cbdcee3b-f57e-5c7b-cf27-83892fb811c4)
- 开源项目（GitHub API 实测 2026-08-12）：[MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)、[TrendRadar](https://github.com/sansan0/TrendRadar)、[BettaFish/微舆](https://github.com/666ghj/BettaFish)、[DailyHotApi](https://github.com/imsyy/DailyHotApi)、[weibo-search](https://github.com/dataabc/weibo-search)、[RSSHub](https://github.com/DIYgod/RSSHub)、[weibo-trending-hot-history](https://github.com/lxw15337674/weibo-trending-hot-history)
- 数据集：[ChineseNlpCorpus/weibo_senti_100k](https://github.com/SophonPlus/ChineseNlpCorpus)、[kuroneko5943/weibo16](https://huggingface.co/datasets/kuroneko5943/weibo16)
- 新闻 API：[聚合数据](https://www.juhe.cn/docs/api/id/235)、[天行数据](https://www.tianapi.com/)、[探数数据](https://www.tanshuapi.com/news/detail-107)
- 法律：[微博诉蚁坊鹰击案](https://zhuanlan.zhihu.com/p/547560088)、[新浪诉脉脉案](http://iolaw.cssn.cn/flxw/201605/t20160502_4640475.shtml)、[大众点评诉百度案](https://zhuanlan.zhihu.com/p/114298103)、[北京知产法院涉数据反不正当竞争典型案例](https://www.ipeconomy.cn/index.php/index/news/magazine_details/id/6915.html)、[德恒：抓取公开数据边界](https://www.dehenglaw.com/CN/tansuocontent/0008/025285/7.aspx)、[最高检：爬取数据须遵规](https://www.spp.gov.cn/llyj/202202/t20220210_543998.shtml)、[爬虫合规边界（知乎）](https://zhuanlan.zhihu.com/p/671064577)

> 未能查证/存在不确定：① 各平台"个人开发者具体能拿到哪些接口"以第三方转述和社区反馈为主，官方分级文档需以官网实时为准；② "高频爬取导致封号"为社区普遍反馈，本次未取到逐案实证原文；③ MediaCrawlerPro 系列在 GitHub 公开路径下已查不到对应仓库，判断为已闭源/商业化，具体状态需向作者确认。
