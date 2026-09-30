from datetime import date, datetime, timedelta, timezone

import pytest

from yuqing.core.fetch.base import FetchResult
from yuqing.core.search.base import SearchResult
from yuqing.services.topic_discovery import (
    TopicDiscovery,
    TopicDiscoveryRequest,
)


class StaticProvider:
    capabilities = {"freshness", "publish_time"}

    def __init__(self, name: str, results: list[SearchResult]):
        self.name = name
        self.results = results
        self.calls: list[str] = []

    async def search(self, params):
        self.calls.append(params.query)
        return self.results[: params.top_k]


class StaticSearch:
    name = "static-search"
    capabilities = {"freshness", "publish_time"}

    def __init__(self, providers):
        self.providers = providers

    async def search(self, params):
        return await self.providers[0].search(params)


class StaticFetcher:
    def __init__(self, pages: dict[str, str] | None = None):
        self.pages = pages or {}
        self.calls: list[str] = []

    async def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        if url not in self.pages:
            raise RuntimeError("fixture page unavailable")
        text = self.pages[url]
        return FetchResult(
            url=url, html=f"<main>{text}</main>", content_text=text, content_type="text/html"
        )


class StaticPlanner:
    async def propose(self, topic, *, date_from, date_to, language, limit):
        core = topic.removesuffix("舆情")
        return [f"{core} 食堂异物投诉", f"{core} 宿舍冲突视频"][:limit]


@pytest.mark.asyncio
async def test_initial_discovery_uses_specific_planned_query_before_generic_templates():
    free = StaticProvider("langsearch", [])
    discovery = TopicDiscovery(StaticSearch([free]), StaticFetcher(), planner=StaticPlanner())

    await discovery.discover(
        TopicDiscoveryRequest(topic="武汉大学舆情", max_search_calls=1, max_fetch_calls=0)
    )

    assert free.calls == ["武汉大学 食堂异物投诉"]


class HintRoutingProvider(StaticProvider):
    def __init__(self, stale: SearchResult, current: SearchResult):
        super().__init__("only", [])
        self.stale = stale
        self.current = current

    async def search(self, params):
        self.calls.append(params.query)
        if "图书馆争议事件 后续" in params.query:
            return [self.current]
        if len(self.calls) == 1:
            return [self.stale]
        return []


def result(url: str, title: str, snippet: str, *, source_name: str | None = None):
    return SearchResult(
        url=url,
        title=title,
        snippet=snippet,
        source_name=source_name,
        published_at=datetime(2025, 9, 20),
        provider="fixture",
        lang="zh",
        raw={"date_provenance": "trusted_structured"},
    )


@pytest.mark.asyncio
async def test_wuhan_university_results_become_one_supported_event_cluster():
    official = result(
        "https://www.whu.edu.cn/info/5231/258444.htm",
        "情况通报",
        "武汉大学对图书馆事件纪律处分和学位论文问题开展调查复核。",
        source_name="武汉大学",
    )
    report = result(
        "https://www.xinhuanet.com/example/wuda-library.html",
        "武大回应图书馆事件：启动调查复核",
        "武汉大学回应图书馆争议，并公布后续处置安排。",
        source_name="新华社",
    )
    court = result(
        "https://www.jiemian.com/article/example.html",
        "图书馆性骚扰纠纷一审宣判",
        "武汉大学图书馆事件一审判决驳回全部诉讼请求。",
        source_name="界面新闻",
    )
    routine = result(
        "https://sud.whu.edu.cn/info/1511/16851.htm",
        "城市设计学院关于推荐参加2023年武汉大学英诺大学生创新成果奖评选公示",
        "武汉大学校内评奖通知。",
        source_name="武汉大学城市设计学院",
    )
    unrelated = result(
        "https://xxgk.hubu.edu.cn/example.html",
        "湖北大学新生复查处理结果",
        "湖北大学信息公开。",
        source_name="湖北大学",
    )
    providers = [
        StaticProvider("primary", [official, routine, unrelated]),
        StaticProvider("secondary", [report, court]),
    ]
    discovery = TopicDiscovery(StaticSearch(providers), StaticFetcher())

    outcome = await discovery.discover(
        TopicDiscoveryRequest(
            topic="武汉大学舆情",
            languages=("zh",),
            source_scope="domestic",
            date_from="2023-01-01",
            date_to="2026-01-01",
            max_search_calls=8,
            max_fetch_calls=4,
        )
    )

    assert outcome.candidates
    candidate = outcome.candidates[0]
    assert "图书馆" in candidate.title
    assert candidate.confidence == "confirmed"
    assert candidate.source_count >= 2
    assert {source.provider for source in candidate.sources} == {"primary", "secondary"}
    assert all("成果奖" not in item.title for item in outcome.candidates)
    assert outcome.provider_coverage.configured == 2
    assert outcome.provider_coverage.limited is False


@pytest.mark.asyncio
async def test_candidate_sources_allow_mixed_timezone_and_missing_publish_dates():
    known_date = result(
        "https://www.whu.edu.cn/notice-aware.html",
        "武汉大学通报图书馆争议事件",
        "武汉大学就图书馆争议发布情况通报。",
        source_name="武汉大学",
    ).model_copy(
        update={"published_at": datetime(2025, 9, 20, 8, tzinfo=timezone(timedelta(hours=8)))}
    )
    missing_date = result(
        "https://www.news.cn/whu-undated.html",
        "武大回应图书馆争议事件",
        "媒体报道武汉大学已发布图书馆争议情况通报。",
        source_name="新华社",
    ).model_copy(update={"published_at": None})
    naive_date = result(
        "https://www.thepaper.cn/whu-naive.html",
        "武汉大学图书馆争议事件后续",
        "媒体跟进武汉大学图书馆争议事件处置进展。",
        source_name="澎湃新闻",
    ).model_copy(update={"published_at": datetime(2025, 9, 21, 8)})

    outcome = await TopicDiscovery(
        StaticSearch(
            [
                StaticProvider("primary", [known_date]),
                StaticProvider("secondary", [missing_date, naive_date]),
            ]
        ),
        StaticFetcher(),
    ).discover(
        TopicDiscoveryRequest(
            topic="武汉大学舆情",
            languages=("zh",),
            source_scope="domestic",
            date_from="2025-01-01",
            date_to="2026-01-01",
            max_search_calls=8,
            max_fetch_calls=0,
        )
    )

    assert outcome.candidates[0].sources[0].published_at == "2025-09-20T08:00:00+08:00"
    assert outcome.candidates[0].sources[1].published_at == "2025-09-21T08:00:00"
    assert outcome.candidates[0].sources[2].published_at is None


@pytest.mark.asyncio
async def test_discovery_stops_before_third_provider_after_two_productive_paths():
    first = StaticProvider(
        "langsearch",
        [
            result(
                "https://www.whu.edu.cn/notice.html",
                "武汉大学通报图书馆争议事件",
                "武汉大学就图书馆争议发布情况通报。",
            )
        ],
    )
    second = StaticProvider(
        "qianfan",
        [
            result(
                "https://www.news.cn/whu.html",
                "武大回应图书馆争议",
                "媒体报道武汉大学已经发布情况通报。",
            )
        ],
    )
    reserve = StaticProvider(
        "bocha",
        [
            result(
                "https://example.cn/reserve.html",
                "武大图书馆争议后续",
                "储备搜索路径返回的报道。",
            )
        ],
    )

    outcome = await TopicDiscovery(
        StaticSearch([first, second, reserve]), StaticFetcher()
    ).discover(
        TopicDiscoveryRequest(
            topic="武汉大学舆情",
            languages=("zh",),
            source_scope="domestic",
            date_from="2023-01-01",
            date_to="2026-01-01",
            max_search_calls=8,
            max_fetch_calls=0,
        )
    )

    assert outcome.candidates
    assert first.calls
    assert second.calls
    assert reserve.calls == []
    assert outcome.provider_coverage.successful == ("langsearch", "qianfan")


@pytest.mark.asyncio
async def test_empty_http_response_is_not_counted_as_effective_provider_coverage():
    empty = StaticProvider("langsearch", [])
    productive = StaticProvider(
        "qianfan",
        [
            result(
                "https://www.whu.edu.cn/notice.html",
                "武汉大学通报图书馆争议事件",
                "武汉大学就图书馆争议发布情况通报。",
            )
        ],
    )

    outcome = await TopicDiscovery(StaticSearch([empty, productive]), StaticFetcher()).discover(
        TopicDiscoveryRequest(
            topic="武汉大学舆情",
            languages=("zh",),
            source_scope="domestic",
            date_from="2023-01-01",
            date_to="2026-01-01",
            max_search_calls=4,
            max_fetch_calls=0,
        )
    )

    assert outcome.provider_coverage.successful == ("qianfan",)
    assert outcome.provider_coverage.limited is True


@pytest.mark.asyncio
async def test_subject_may_come_from_snippet_but_routine_notice_is_still_rejected():
    provider = StaticProvider(
        "only",
        [
            result(
                "https://news.example.cn/court.html",
                "图书馆性骚扰纠纷一审宣判",
                "武汉大学图书馆事件一审判决公布。",
            ),
            result(
                "https://news.example.cn/award.html",
                "2025年度优秀成果评选公示",
                "武汉大学发布校内奖项名单。",
            ),
            result(
                "https://xxgk.hubu.edu.cn/footer-match.html",
                "湖北大学新生复查调查结果",
                "湖北大学信息公开页面。" + "普通正文。" * 80 + "相关链接：武汉大学。",
            ),
        ],
    )
    discovery = TopicDiscovery(StaticSearch([provider]), StaticFetcher())

    outcome = await discovery.discover(
        TopicDiscoveryRequest(
            topic="武汉大学舆情",
            languages=("zh",),
            source_scope="domestic",
            date_from="2023-01-01",
            date_to="2026-01-01",
            max_search_calls=4,
            max_fetch_calls=0,
        )
    )

    assert len(outcome.candidates) == 1
    assert "一审" in outcome.candidates[0].title
    assert outcome.candidates[0].confidence == "lead"
    assert outcome.provider_coverage.limited is True


@pytest.mark.asyncio
async def test_empty_discovery_explains_both_initial_and_recovery_attempts():
    provider = StaticProvider(
        "only",
        [result("https://www.whu.edu.cn/about.html", "武汉大学新闻网", "学校首页")],
    )
    discovery = TopicDiscovery(StaticSearch([provider]), StaticFetcher())

    outcome = await discovery.discover(
        TopicDiscoveryRequest(
            topic="武汉大学舆情",
            languages=("zh",),
            source_scope="domestic",
            date_from="2023-01-01",
            date_to="2026-01-01",
            max_search_calls=8,
            max_fetch_calls=0,
        )
    )

    assert outcome.candidates == ()
    assert {attempt.round for attempt in outcome.attempts} == {"initial", "recovery"}
    assert sum(attempt.rejected.get("generic_or_routine", 0) for attempt in outcome.attempts) > 0
    assert outcome.provider_coverage.limited is True


@pytest.mark.asyncio
async def test_post_fetch_date_rejection_triggers_the_bounded_recovery_round():
    stale_url = "https://www.whu.edu.cn/stale-incident.html"
    provider = StaticProvider(
        "only",
        [
            result(
                stale_url,
                "武汉大学通报图书馆争议事件",
                "武汉大学对图书馆争议事件开展调查。",
            )
        ],
    )
    discovery = TopicDiscovery(
        StaticSearch([provider]),
        StaticFetcher({stale_url: "发布时间：2024年7月12日 武汉大学通报调查结果。"}),
    )

    outcome = await discovery.discover(
        TopicDiscoveryRequest(
            topic="武汉大学舆情",
            languages=("zh",),
            source_scope="domestic",
            date_from="2025-01-01",
            date_to="2026-01-01",
            max_search_calls=5,
            max_fetch_calls=3,
        )
    )

    assert outcome.candidates == ()
    assert {attempt.round for attempt in outcome.attempts} == {"initial", "recovery"}
    assert (
        sum(
            attempt.rejected.get("outside_time_window_after_fetch", 0)
            for attempt in outcome.attempts
        )
        == 1
    )


@pytest.mark.asyncio
async def test_out_of_window_provider_date_is_excluded_even_with_provider_fulltext():
    outside = result(
        "https://news.example.cn/future.html",
        "武汉大学通报图书馆争议事件",
        "武汉大学公布图书馆争议事件后续情况。",
    ).model_copy(
        update={
            "published_at": datetime(2026, 9, 20),
            "content_text": "武汉大学图书馆争议事件后续。" * 40,
            "content_origin": "provider_fulltext",
            "raw": {"date_provenance": "search_provider"},
        }
    )
    provider = StaticProvider("only", [outside])

    outcome = await TopicDiscovery(StaticSearch([provider]), StaticFetcher()).discover(
        TopicDiscoveryRequest(
            topic="武汉大学舆情",
            languages=("zh",),
            source_scope="domestic",
            date_from="2023-01-01",
            date_to="2026-01-01",
            max_search_calls=3,
            max_fetch_calls=0,
        )
    )

    assert outcome.candidates == ()
    assert (
        sum(
            attempt.rejected.get("outside_time_window_after_fetch", 0)
            for attempt in outcome.attempts
        )
        >= 1
    )


@pytest.mark.asyncio
async def test_recovery_uses_rejected_event_evidence_to_find_an_in_range_official_update():
    stale_url = "https://www.whu.edu.cn/stale-library-event.html"
    current_url = "https://www.whu.edu.cn/current-library-review.html"
    provider = HintRoutingProvider(
        result(
            stale_url,
            "武汉大学图书馆争议事件",
            "武汉大学已经开展调查。",
        ),
        result(
            current_url,
            "武大通报图书馆事件调查复核情况",
            "武汉大学公布图书馆争议事件调查复核和处分结果。",
            source_name="武汉大学",
        ),
    )
    discovery = TopicDiscovery(
        StaticSearch([provider]),
        StaticFetcher({stale_url: "发布时间：2024年7月12日 武汉大学启动调查。"}),
    )

    outcome = await discovery.discover(
        TopicDiscoveryRequest(
            topic="武汉大学舆情",
            languages=("zh",),
            source_scope="domestic",
            date_from="2025-01-01",
            date_to="2026-01-01",
            max_search_calls=8,
            max_fetch_calls=3,
        )
    )

    assert outcome.candidates
    assert "图书馆" in outcome.candidates[0].title
    assert outcome.candidates[0].confidence == "lead"
    assert provider.calls[3].startswith("武汉大学图书馆争议事件 后续")


@pytest.mark.asyncio
async def test_recovery_reserves_official_queries_before_model_suggestions():
    provider = StaticProvider("only", [])
    discovery = TopicDiscovery(StaticSearch([provider]), StaticFetcher(), planner=StaticPlanner())

    await discovery.discover(
        TopicDiscoveryRequest(
            topic="武汉大学舆情",
            languages=("zh",),
            max_search_calls=8,
            max_fetch_calls=0,
        )
    )

    recovery_calls = provider.calls[3:]
    assert recovery_calls[:2] == [
        "武汉大学 调查复核 结果 处分",
        "武汉大学 官方 最新 通报 回应",
    ]
    assert "食堂异物" in recovery_calls[-1]


@pytest.mark.asyncio
async def test_manual_preflight_keeps_an_unverified_input_as_a_lead_not_an_event():
    provider = StaticProvider("only", [])
    discovery = TopicDiscovery(StaticSearch([provider]), StaticFetcher())

    outcome = await discovery.discover(
        TopicDiscoveryRequest(
            topic="武汉大学舆情",
            manual_event_query="武汉大学图书馆事件",
            languages=("zh",),
            source_scope="domestic",
            date_from="2023-01-01",
            date_to="2026-01-01",
            max_search_calls=4,
            max_fetch_calls=0,
        )
    )

    assert outcome.candidates == ()
    assert outcome.manual_preflight is True
    assert outcome.attempts


@pytest.mark.asyncio
async def test_missing_time_range_defaults_to_the_last_twelve_months():
    discovery = TopicDiscovery(
        StaticSearch([StaticProvider("only", [])]),
        StaticFetcher(),
        today=lambda: date(2026, 9, 21),
    )

    outcome = await discovery.discover(
        TopicDiscoveryRequest(
            topic="武汉大学舆情",
            languages=("zh",),
            source_scope="auto",
            max_search_calls=1,
            max_fetch_calls=0,
        )
    )

    assert outcome.used_default_time_range is True
    assert outcome.effective_time_range.date_from == "2025-09-20"
    assert outcome.effective_time_range.date_to == "2026-09-21"


@pytest.mark.asyncio
async def test_fixed_real_incident_replay_meets_candidate_acceptance_metrics():
    cases = [
        (
            "武汉大学舆情",
            "图书馆",
            [
                result(
                    "https://www.xinhuanet.com/politics/20250920/e79479d9068844458b12ba720a9615f7/c.html",
                    "武大通报图书馆事件调查复核情况",
                    "武汉大学公布图书馆争议事件调查复核和处分结果。",
                    source_name="新华社",
                ),
                result(
                    "https://www.thepaper.cn/newsDetail_forward_24924727",
                    "武汉大学通报学生图书馆性骚扰争议事件",
                    "武汉大学成立工作组调查图书馆相关举报。",
                    source_name="澎湃新闻",
                ),
            ],
            result(
                "https://sud.whu.edu.cn/notice/award.html",
                "武汉大学大学生创新成果奖评选公示",
                "武汉大学校内评奖通知。",
            ),
        ),
        (
            "小米汽车舆情",
            "召回",
            [
                result(
                    "https://www.samr.gov.cn/zw/zh/art/2025/art_589f94caf6ad48a484fcf97135ca3eb5.html",
                    "小米汽车科技有限公司召回部分SU7标准版电动汽车",
                    "市场监管总局公布小米汽车SU7标准版召回计划。",
                    source_name="国家市场监督管理总局",
                ),
                result(
                    "https://www.mi.com/service/exchange#phone",
                    "小米汽车回应SU7标准版召回安排",
                    "小米汽车说明召回原因和软件升级安排。",
                    source_name="小米汽车",
                ),
            ],
            result(
                "https://www.mi.com/event/launch.html",
                "小米汽车新品活动预告",
                "小米汽车新品发布活动通知。",
            ),
        ),
        (
            "天水市舆情",
            "血铅",
            [
                result(
                    "https://www.maiji.gov.cn/info/18871/555012.htm",
                    "天水市通报幼儿园幼儿血铅异常事件",
                    "天水市成立联合工作组调查幼儿园违规使用添加剂问题。",
                    source_name="麦积区人民政府",
                ),
                result(
                    "https://m.12371.gov.cn/content/2025-07/20/content_495616.html",
                    "天水幼儿园血铅异常问题调查处置情况通报",
                    "甘肃调查组公布天水市涉事幼儿园调查处置结果。",
                    source_name="甘肃省政府网站",
                ),
            ],
            result(
                "https://www.tianshui.gov.cn/notice/meeting.html",
                "天水市食品安全工作会议通知",
                "天水市例行会议通知。",
            ),
        ),
        (
            "董宇辉舆情",
            "离职",
            [
                result(
                    "https://news.bjd.com.cn/2024/07/25/10845860.shtml",
                    "董宇辉离职东方甄选",
                    "东方甄选公告董宇辉离职并收购与辉同行股权。",
                    source_name="北京日报",
                ),
                result(
                    "https://www.yicai.com/news/102245327.html",
                    "董宇辉离职后东方甄选发布公告回应",
                    "东方甄选披露董宇辉离职及与辉同行股权安排。",
                    source_name="第一财经",
                ),
            ],
            result(
                "https://news.example.cn/dong-live.html",
                "董宇辉直播活动预告",
                "董宇辉参加例行直播活动。",
            ),
        ),
    ]
    recalled = emitted = unrelated = routine_notices = 0

    for topic, expected_token, event_results, routine in cases:
        unrelated_result = result(
            "https://news.example.cn/unrelated.html",
            "另一机构事故调查通报",
            "另一机构对事故作出回应。",
        )
        discovery = TopicDiscovery(
            StaticSearch(
                [
                    StaticProvider("primary", [event_results[0], routine, unrelated_result]),
                    StaticProvider("secondary", [event_results[1]]),
                ]
            ),
            StaticFetcher(),
        )
        outcome = await discovery.discover(
            TopicDiscoveryRequest(
                topic=topic,
                languages=("zh",),
                source_scope="auto",
                date_from="2023-01-01",
                date_to="2026-01-01",
                max_search_calls=8,
                max_fetch_calls=0,
            )
        )
        titles = [candidate.title for candidate in outcome.candidates]
        recalled += int(any(expected_token in title for title in titles))
        emitted += len(titles)
        unrelated += sum(expected_token not in title for title in titles)
        routine_notices += sum("预告" in title or "评选公示" in title for title in titles)

    assert recalled / len(cases) >= 0.8
    assert unrelated / max(1, emitted) <= 0.05
    assert routine_notices == 0
