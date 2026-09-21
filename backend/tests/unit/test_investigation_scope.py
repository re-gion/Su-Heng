from datetime import datetime

from yuqing.agents.runtime import InvestigationPlan, SearchQuery
from yuqing.core.search.base import SearchResult
from yuqing.services.evidence_store import extract_page_published_at
from yuqing.services.investigation_scope import (
    InvestigationScope,
    ReportReleaseAssessment,
    is_concrete_event_candidate,
    is_topic_discovery_query,
    title_matches_subject,
)
from yuqing.services.v1_orchestrator import ensure_requested_languages, valid_media_analysis


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


def test_topic_candidate_rejects_navigation_pages_but_keeps_incident_headlines():
    assert is_concrete_event_candidate("媒体武大") is False
    assert is_concrete_event_candidate("武汉大学新闻网") is False
    assert is_concrete_event_candidate("武汉大学发布图书馆事件情况说明") is True
    assert title_matches_subject("武汉大学舆情", "武汉大学发布图书馆事件情况说明") is True
    assert title_matches_subject("武汉大学舆情", "2023年无此类情况-湖北大学信息公开网") is False


def test_primary_scope_rejects_wrong_language_foreign_and_out_of_window_results():
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
            title="武汉大学发布情况说明",
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
            title="武汉大学图书馆后续文章",
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
    assert outside.main_eligible is False
    assert outside.bucket == "background"
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
            title="武汉大学发布情况说明",
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


def test_page_date_extraction_only_promotes_structured_or_labeled_dates():
    structured = extract_page_published_at(
        '<script type="application/ld+json">{"datePublished":"2025-09-20T08:30:00+08:00"}</script>',
        "文章正文",
    )
    visible = extract_page_published_at("<html></html>", "发布时间：2024年7月12日 正文")
    unlabeled = extract_page_published_at("<footer>Copyright 2025</footer>", "2025 年度回顾")

    assert structured == ("2025-09-20T08:30:00+08:00", "page_metadata")
    assert visible == ("2024-07-12T00:00:00", "page_visible")
    assert unlabeled == (None, None)


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


def test_full_report_release_gate_requires_fact_timeline_media_edge_and_actionable_summary():
    incomplete = ReportReleaseAssessment.evaluate(
        concrete_event=True,
        main_evidence=4,
        verifiable_key_claims=1,
        in_window_timeline_nodes=2,
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
        in_window_timeline_nodes=3,
        publication_nodes=2,
        propagation_edges=1,
        summary_has_what=True,
        summary_has_why=True,
        summary_has_action=True,
        evidence_bound_recommendations=1,
    )

    assert incomplete.label == "evidence_brief"
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
