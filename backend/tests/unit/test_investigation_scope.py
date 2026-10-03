from datetime import datetime

from yuqing.agents.runtime import InvestigationPlan, SearchQuery
from yuqing.core.search.base import SearchResult
from yuqing.services.evidence_store import extract_page_published_at
from yuqing.services.investigation_scope import (
    InvestigationScope,
    ReportReleaseAssessment,
    is_domestic_url,
    is_topic_discovery_query,
    title_matches_subject,
)
from yuqing.services.v1_orchestrator import ensure_requested_languages, valid_media_analysis


def test_chinese_publication_date_beside_source_in_article_header():
    page = (
        '<h1>公开通报</h1><div class="info"><span id="author"></span>'
        '<span class="source">新闻机构</span><span>2025年09月20日 09:57</span></div>'
    )
    assert extract_page_published_at(page, "报道正文") == ("2025-09-20T09:57:00", "page_visible")
    sidebar = page.replace("<h1>公开通报</h1>", "<aside>") + "</aside>"
    assert extract_page_published_at(sidebar, "报道正文") == (None, None)


def result(
    *,
    title: str,
    url: str,
    published_at: str | None,
    lang: str = "zh",
    raw: dict | None = None,
) -> SearchResult:
    return SearchResult(
        url=url,
        title=title,
        snippet=title,
        published_at=datetime.fromisoformat(published_at) if published_at else None,
        provider="fixture",
        lang=lang,
        raw=raw or {},
    )


def test_requested_languages_are_a_hard_allowlist():
    plan = InvestigationPlan(
        queries=[
            SearchQuery(query="武汉大学 官方回应", language="zh", region="CN"),
            SearchQuery(query="Wuhan University controversy", language="en", region="US"),
            SearchQuery(query="武漢大学", language="ja", region="JP"),
        ]
    )

    normalized = ensure_requested_languages(plan, ["zh"], "武汉大学舆情")

    assert [item.language for item in normalized.queries] == ["zh"]
    assert [item.query for item in normalized.queries] == ["武汉大学 官方回应"]


def test_broad_public_opinion_query_requires_event_selection():
    assert is_topic_discovery_query("武汉大学舆情") is True
    assert is_topic_discovery_query("武汉大学图书馆性骚扰指控及校方回应") is False


def test_topic_subject_matching_and_domestic_hosts_are_deterministic():
    assert title_matches_subject("武汉大学舆情", "武汉大学发布图书馆事件情况说明") is True
    assert title_matches_subject("武汉大学舆情", "2023年无此类情况-湖北大学信息公开网") is False
    assert title_matches_subject("董宇辉舆情", "董宇辉离职东方甄选", fallback_to_query=True) is True
    assert (
        title_matches_subject("董宇辉舆情", "另一位主播宣布离职", fallback_to_query=True) is False
    )
    assert is_domestic_url("https://www.toutiao.com/article/123") is True
    assert is_domestic_url("https://s.cyol.com/articles/123") is True
    assert is_domestic_url("https://www.yangtse.com/news/123") is True
    assert is_domestic_url("https://example.com/article/123") is False


def test_primary_scope_rejects_wrong_language_and_labels_out_of_window_event_context():
    scope = InvestigationScope(
        event_query="武汉大学图书馆事件",
        subject_aliases=("Wuhan University",),
        languages=("zh",),
        source_scope="domestic",
        date_from="2023-01-01",
        date_to="2026-01-01",
    )

    accepted = scope.classify_result(
        result(
            title="武汉大学发布图书馆事件情况说明",
            url="https://www.whu.edu.cn/info/5231/258444.htm",
            published_at="2025-09-20T08:00:00+08:00",
            raw={"date_provenance": "page_metadata"},
        ),
        agent="fact_investigator",
    )
    foreign = scope.classify_result(
        result(
            title="Wuhan University Library",
            url="https://en.wikipedia.org/wiki/Wuhan_University_Library",
            published_at="2025-09-20T08:00:00+08:00",
            lang="en",
            raw={"date_provenance": "page_metadata"},
        ),
        agent="fact_investigator",
    )
    outside = scope.classify_result(
        result(
            title="武汉大学图书馆事件后续调查",
            url="https://news.whu.edu.cn/info/1002/99999.htm",
            published_at="2026-06-04T08:00:00+08:00",
            raw={"date_provenance": "page_metadata"},
        ),
        agent="fact_investigator",
    )

    assert accepted.main_eligible is True
    assert accepted.bucket == "main"
    assert foreign.accepted is False
    assert {"language_not_allowed", "foreign_source_in_domestic_phase"} <= set(foreign.reasons)
    assert outside.accepted is True
    assert outside.main_eligible is True
    assert outside.bucket == "event_context"
    assert "outside_time_window" in outside.reasons


def test_untrusted_provider_date_is_pending_even_when_hint_is_outside_window():
    scope = InvestigationScope(
        event_query="武汉大学图书馆事件",
        languages=("zh",),
        source_scope="domestic",
        date_from="2023-01-01",
        date_to="2026-01-01",
    )

    decision = scope.classify_result(
        result(
            title="武汉大学发布图书馆事件情况说明",
            url="https://www.whu.edu.cn/info/5231/258444.htm",
            published_at="2026-08-18T08:00:00+08:00",
            raw={"date_provenance": "search_provider"},
        ),
        agent="fact_investigator",
    )

    assert decision.accepted is True
    assert decision.bucket == "pending"
    assert decision.main_eligible is False
    assert decision.reasons == ("date_untrusted",)


def test_history_scope_allows_distinct_prior_event_at_same_institution():
    scope = InvestigationScope(
        event_query="武汉大学图书馆事件",
        languages=("zh",),
        date_from="2025-07-01",
        date_to="2025-07-31",
    )
    prior = scope.classify_result(
        result(
            title="武汉大学公布另一学生处分争议复核结果",
            url="https://www.whu.edu.cn/other-case",
            published_at="2023-06-01T08:00:00+08:00",
            raw={"date_provenance": "page_metadata"},
        ),
        agent="history_insight",
    )
    current = scope.classify_result(
        result(
            title="武汉大学图书馆事件处分复核结果",
            url="https://www.whu.edu.cn/current-case",
            published_at="2023-06-01T08:00:00+08:00",
            raw={"date_provenance": "page_metadata"},
        ),
        agent="history_insight",
    )
    assert prior.bucket == "history"
    assert prior.reasons == ("outside_time_window",)
    assert current.accepted is False
    assert current.reasons == ("current_event_not_history",)


def test_concrete_event_scope_rejects_footer_only_subject_match():
    scope = InvestigationScope(
        event_query="武汉大学通报图书馆事件调查复核情况",
        languages=("zh",),
        source_scope="domestic",
        date_from="2023-01-01",
        date_to="2026-01-01",
    )
    unrelated = SearchResult(
        url="https://xxgk.hubu.edu.cn/info/1234/5678.htm",
        title="新生复查期间有关举报、调查及处理结果（2023年）",
        snippet=(
            "湖北大学新生复查正常完成，期间未接到举报。"
            + "普通正文。" * 80
            + "相关链接：武汉大学。"
        ),
        provider="fixture",
        lang="zh",
    )

    decision = scope.classify_result(unrelated, agent="fact_investigator")

    assert decision.accepted is False
    assert decision.reasons == ("subject_mismatch",)


def test_concrete_event_scope_requires_event_specific_context_not_only_same_subject():
    scope = InvestigationScope(
        event_query="武汉大学通报图书馆事件调查复核情况",
        languages=("zh",),
        source_scope="domestic",
    )
    unrelated = SearchResult(
        url="https://news.whu.edu.cn/info/1002/99999.htm",
        title="武汉大学科研基地挂牌",
        snippet="武汉大学泰康医学院举行科研基地挂牌签约仪式。",
        provider="fixture",
        lang="zh",
    )

    decision = scope.classify_result(unrelated, agent="fact_investigator")

    assert decision.accepted is False
    assert decision.reasons == ("event_mismatch",)


def test_event_context_does_not_match_a_fragment_inside_an_unrelated_topic():
    scope = InvestigationScope(
        event_query="武汉大学通报图书馆事件调查复核情况",
        languages=("zh",),
        source_scope="auto",
    )
    unrelated = SearchResult(
        url="https://zh.wikipedia.org/wiki/example-history-dispute",
        title="武汉大学校史争议",
        snippet=(
            "刘经南认为，地点、校舍、设备、档案和图书资料的继承，"
            "以及少量师资的延续体现了一脉相承。武汉大学校方回应称，有大量史实。"
        ),
        provider="langsearch",
        lang="zh",
    )

    decision = scope.classify_result(
        unrelated,
        agent="fact_investigator",
        search_query="武汉大学 图书馆 争议 校方 回应",
    )

    assert decision.accepted is False
    assert decision.reasons == ("event_mismatch",)


def test_same_incident_court_update_need_not_repeat_official_notice_verbs():
    scope = InvestigationScope(
        event_query="武汉大学通报图书馆事件调查复核情况",
        languages=("zh",),
        source_scope="auto",
    )
    court_update = SearchResult(
        url="https://www.zaobao.com.sg/realtime/china/story20250920-7542836",
        title="法院驳回武大图书馆事件女生上诉 维持原判",
        snippet=(
            "事发2023年10月，武大女学生在社媒发文称，"
            "当年7月在图书馆自习时受到性骚扰。她因此指控男方行为。"
        ),
        provider="langsearch",
        lang="zh",
    )

    decision = scope.classify_result(
        court_update,
        agent="fact_investigator",
        search_query="武汉大学 图书馆 争议 校方 回应",
    )

    assert decision.accepted is True


def test_history_scope_accepts_other_incident_without_current_subject():
    scope = InvestigationScope(
        event_query="武汉大学通报图书馆事件调查复核情况",
        languages=("zh",),
        source_scope="domestic",
    )
    other_case = result(
        title="某高校公布学生投诉调查和纪律处分结果",
        url="https://example.cn/other-case",
        published_at="2024-04-12T08:00:00+08:00",
    )
    current_case = result(
        title="武汉大学通报图书馆事件调查复核情况",
        url="https://example.cn/current-case",
        published_at="2025-09-20T08:00:00+08:00",
    )

    other = scope.classify_result(
        other_case,
        agent="history_insight",
        search_query="高校 学生投诉 调查 纪律处分 历史案例",
    )
    current = scope.classify_result(
        current_case,
        agent="history_insight",
        search_query="高校 学生投诉 调查 纪律处分 历史案例",
    )

    assert other.accepted is True
    assert other.bucket == "history"
    assert current.accepted is False


def test_english_history_search_rejects_generic_pages_without_spending_next_provider():
    scope = InvestigationScope(
        event_query="武汉大学通报图书馆事件调查复核情况",
        languages=("zh",),
        source_scope="domestic",
    )
    comparable = result(
        title="A university publishes findings after a student misconduct investigation",
        url="https://example.org/comparable-case",
        published_at="2024-04-12T08:00:00+00:00",
        lang="en",
    )
    navigation = result(
        title="University news and events",
        url="https://example.org/news",
        published_at="2024-04-12T08:00:00+00:00",
        lang="en",
    )

    accepted = scope.classify_result(
        comparable,
        agent="history_insight",
        phase="foreign_supplement",
        search_query="university student misconduct investigation outcome case",
    )
    rejected = scope.classify_result(
        navigation,
        agent="history_insight",
        phase="foreign_supplement",
        search_query="university student misconduct investigation outcome case",
    )

    assert accepted.accepted is True
    assert accepted.bucket == "history"
    assert rejected.accepted is False


def test_foreign_event_uses_explicit_subject_alias_without_admitting_other_universities():
    scope = InvestigationScope(
        event_query="武汉大学通报图书馆事件调查复核情况",
        languages=("zh",),
        source_scope="domestic",
        subject_aliases=("Wuhan University",),
    )
    relevant = result(
        title="Wuhan University reviews library harassment allegations",
        url="https://example.org/wuhan-library",
        published_at="2025-09-20T08:00:00+00:00",
        lang="en",
    )
    unrelated = result(
        title="Hubei University reviews library harassment allegations",
        url="https://example.org/hubei-library",
        published_at="2025-09-20T08:00:00+00:00",
        lang="en",
    )
    query = "Wuhan University library incident investigation review announcement"

    assert scope.classify_result(
        relevant, agent="fact_investigator", phase="foreign_supplement", search_query=query
    ).accepted
    assert not scope.classify_result(
        unrelated, agent="fact_investigator", phase="foreign_supplement", search_query=query
    ).accepted


def test_provider_fulltext_lead_can_rescue_event_hidden_by_short_snippet():
    scope = InvestigationScope(
        event_query="武汉大学通报图书馆事件调查复核情况",
        languages=("zh",),
        source_scope="domestic",
    )
    item = SearchResult(
        url="https://news.whu.edu.cn/review",
        title="武汉大学发布最新情况",
        snippet="学校发布情况通报。",
        content_text="武汉大学通报图书馆事件调查复核情况。" + "调查过程与结论。" * 30,
        provider="fixture",
        lang="zh",
    )

    assert scope.classify_result(item, agent="fact_investigator").accepted


def test_same_institution_library_forum_does_not_pass_incident_gate():
    scope = InvestigationScope(
        event_query="武汉大学通报图书馆事件调查复核情况",
        languages=("zh",),
        source_scope="domestic",
    )
    unrelated = result(
        title="武汉大学图书馆学博士生论坛征文通知",
        url="https://sim.whu.edu.cn/forum",
        published_at="2025-09-20T08:00:00+08:00",
    )

    decision = scope.classify_result(unrelated, agent="fact_investigator")

    assert decision.accepted is False
    assert decision.reasons == ("event_mismatch",)


def test_english_same_institution_other_incident_needs_library_context():
    scope = InvestigationScope(
        event_query="武汉大学通报图书馆事件调查复核情况",
        languages=("zh",),
        source_scope="domestic",
        subject_aliases=("Wuhan University",),
    )
    unrelated = result(
        title="Wuhan University reviews an admissions misconduct allegation",
        url="https://example.org/other-investigation",
        published_at="2025-09-20T08:00:00+00:00",
        lang="en",
    )

    decision = scope.classify_result(
        unrelated,
        agent="fact_investigator",
        phase="foreign_supplement",
        search_query="Wuhan University library incident investigation review announcement",
    )

    assert decision.accepted is False
    assert decision.reasons == ("event_mismatch",)


def test_english_result_can_match_event_from_provider_text_lead():
    scope = InvestigationScope(
        event_query="武汉大学通报图书馆事件调查复核情况",
        languages=("zh",),
        source_scope="domestic",
        subject_aliases=("Wuhan University",),
    )
    article = SearchResult(
        url="https://example.org/review",
        title="Wuhan University issues a new statement",
        snippet="The school released a statement today.",
        content_text="Wuhan University reviewed the library incident and its misconduct allegations.",
        provider="exa",
        lang="en",
    )

    decision = scope.classify_result(
        article,
        agent="fact_investigator",
        phase="foreign_supplement",
        search_query="Wuhan University library incident investigation review announcement",
    )

    assert decision.accepted is True


def test_search_query_cannot_invent_an_english_subject_alias():
    scope = InvestigationScope(
        event_query="武汉大学通报图书馆事件调查复核情况",
        languages=("zh",),
        source_scope="domestic",
    )
    other = result(
        title="Hubei University reviews library misconduct allegations",
        url="https://example.org/other-school",
        published_at="2025-09-20T08:00:00+00:00",
        lang="en",
    )

    decision = scope.classify_result(
        other,
        agent="fact_investigator",
        phase="foreign_supplement",
        search_query="Hubei University library incident investigation",
    )

    assert decision.accepted is False
    assert decision.reasons == ("subject_mismatch",)


def test_page_date_extraction_only_promotes_structured_or_labeled_dates():
    structured = extract_page_published_at(
        '<script type="application/ld+json">{"datePublished":"2025-09-20T08:30:00+08:00"}</script>',
        "文章正文",
    )
    visible = extract_page_published_at("<html></html>", "发布时间：2024年7月12日 正文")
    unlabeled = extract_page_published_at("<footer>Copyright 2025</footer>", "2025 年度回顾")
    publisher_json = extract_page_published_at(
        '<head></head><script type="application/ld+json">{"pubDate":"2025-09-20T10:15:44"}</script>'
        "<body>2026年9月的其他报道</body>",
        "2025年9月17日，法院作出判决。",
    )
    edition_script = extract_page_published_at(
        '<script>showdate("2025年09月21日")</script>',
        "新华社武汉9月20日电，正文没有标注本页发布日期。",
        "https://epaper.example.cn/shtml/scrb/20250921/331476.shtml",
    )
    unrelated_clock = extract_page_published_at(
        '<script>showdate("2025年09月21日")</script>',
        "正文未标注发布日期。",
        "https://news.example.cn/shtml/scrb/20250920/story.shtml",
    )

    assert structured == ("2025-09-20T08:30:00+08:00", "page_metadata")
    assert visible == ("2024-07-12T00:00:00", "page_visible")
    assert unlabeled == (None, None)
    assert publisher_json == ("2025-09-20T10:15:44", "page_metadata")
    assert edition_script == ("2025-09-21T00:00:00", "page_metadata")
    assert unrelated_clock == (None, None)


def test_history_article_date_in_visible_article_header_is_recognized():
    page = (
        "<html><head><title>人大博士生举报导师事件</title></head><body>"
        '<div class="article-header"><h1>人大博士生举报导师事件</h1>'
        '<span class="timer">2024-07-22 20:12</span></div>'
        "<article>学校公布调查结果。</article></body></html>"
    )
    assert extract_page_published_at(page, "学校公布调查结果。") == (
        "2024-07-22T20:12:00",
        "page_visible",
    )


def test_history_article_date_next_to_source_or_in_time_header_is_recognized():
    adjacent_source = (
        '<div class="ant-space-item"><span>2024-07-22 22:22</span></div>'
        '<div class="ant-space-item"><span>来源：澎湃新闻</span></div>'
    )
    time_header = '<div class="time fix"><span>2024-07-22 22:44:27</span></div>'
    assert extract_page_published_at(adjacent_source, "报道正文") == (
        "2024-07-22T22:22:00",
        "page_visible",
    )
    assert extract_page_published_at(time_header, "报道正文") == (
        "2024-07-22T22:44:27",
        "page_visible",
    )


def test_publisher_search_metadata_date_is_recognized_from_saved_page():
    page = (
        '<div class="bd_block" style="display:none">'
        '<span class="bd_block" id="pubtime_baidu">2025-09-20 10:12:49</span>'
        '<span class="bd_block" id="source_baidu">来源：财新网</span></div>'
    )
    assert extract_page_published_at(page, "据新华社消息，通报详情如下。") == (
        "2025-09-20T10:12:49",
        "page_metadata",
    )


def test_foreign_supplement_allows_original_language_but_never_hides_the_label():
    scope = InvestigationScope(
        event_query="武汉大学图书馆事件",
        subject_aliases=("Wuhan University",),
        languages=("zh",),
        source_scope="domestic",
        date_from="2023-01-01",
        date_to="2026-01-01",
    )
    decision = scope.classify_result(
        result(
            title="Wuhan University responds to library allegation",
            url="https://www.reuters.com/world/china/example",
            published_at="2025-09-21T08:00:00+08:00",
            lang="en",
            raw={"date_provenance": "page_metadata"},
        ),
        agent="media_propagation",
        phase="foreign_supplement",
    )

    assert decision.accepted is True
    assert decision.main_eligible is True
    assert decision.bucket == "foreign_supplement"
    assert decision.display_label == "境外补充"


def test_domestic_source_found_during_supplement_remains_domestic_main_evidence():
    scope = InvestigationScope(
        event_query="武汉大学通报图书馆事件调查复核情况",
        languages=("zh",),
        source_scope="domestic",
        date_from="2023-01-01",
        date_to="2026-01-01",
    )
    for url in (
        "https://www.xinhuanet.com/politics/20250920/example.html",
        "https://s.cyol.com/articles/2025-09/20/example.html",
    ):
        article = result(
            title="武汉大学通报图书馆事件调查复核情况",
            url=url,
            published_at="2025-09-20T10:00:00+08:00",
            raw={"date_provenance": "page_metadata"},
        )
        decision = scope.classify_result(
            article, agent="fact_investigator", phase="foreign_supplement"
        )
        assert is_domestic_url(url)
        assert decision.bucket == "main"


def test_full_report_release_gate_requires_fact_timeline_media_edge_and_actionable_summary():
    no_verified_claim = ReportReleaseAssessment.evaluate(
        concrete_event=True,
        main_evidence=4,
        verifiable_key_claims=0,
        event_timeline_nodes=2,
        publication_nodes=4,
        propagation_edges=0,
        summary_has_what=True,
        summary_has_why=True,
        summary_has_action=True,
        evidence_bound_recommendations=1,
    )
    incomplete = ReportReleaseAssessment.evaluate(
        concrete_event=True,
        main_evidence=4,
        verifiable_key_claims=1,
        event_timeline_nodes=2,
        publication_nodes=1,
        propagation_edges=0,
        summary_has_what=True,
        summary_has_why=False,
        summary_has_action=False,
        evidence_bound_recommendations=0,
    )
    complete = ReportReleaseAssessment.evaluate(
        concrete_event=True,
        main_evidence=5,
        verifiable_key_claims=2,
        event_timeline_nodes=3,
        publication_nodes=2,
        propagation_edges=1,
        summary_has_what=True,
        summary_has_why=True,
        summary_has_action=True,
        evidence_bound_recommendations=1,
    )

    assert incomplete.label == "evidence_brief"
    assert no_verified_claim.label == "retrieval_diagnostic"
    assert no_verified_claim.missing == ("verifiable_key_claim", "media_propagation")
    assert "media_propagation" in incomplete.missing
    assert "executive_summary" in incomplete.missing
    assert complete.label == "full_report"
    assert complete.missing == ()


def test_media_output_must_use_typed_publication_nodes_and_known_edges():
    known = {"E001": object(), "E002": object()}

    assert valid_media_analysis({}, known) is False
    assert (
        valid_media_analysis(
            {
                "publication_node": {
                    "evidence_id": "E001",
                    "publisher": "新华社",
                    "node_type": "original",
                    "framing": "报道事件进展",
                },
                "propagation_edges": [
                    {
                        "from_evidence_id": "E001",
                        "to_evidence_id": "E002",
                        "relation": "follow_up",
                    }
                ],
            },
            known,
        )
        is True
    )
