from types import SimpleNamespace

from yuqing.core.history_cards import unique_history_cards
from yuqing.services.history_comparison import independent_case_evidence


def test_repeated_claims_about_one_case_make_one_card_with_all_refs():
    cards = [
        {
            "event_name": "清华大学处置事件",
            "case_type": "analogous",
            "summary": "短述",
            "evidence_refs": ["E025"],
            "claim_ref": "C011",
        },
        {
            "event_name": " 清华大学处置事件 ",
            "case_type": "analogous",
            "summary": "更完整的经过",
            "evidence_refs": ["E026"],
            "claim_ref": "C012",
        },
        {"event_name": "另一所学校的事件", "case_type": "analogous", "evidence_refs": ["E027"]},
    ]

    result = unique_history_cards(cards)

    assert len(result) == 2
    assert result[0]["summary"] == "更完整的经过"
    assert result[0]["evidence_refs"] == ["E025", "E026"]
    assert result[0]["claim_refs"] == ["C011", "C012"]


def claim(case):
    return SimpleNamespace(analysis_data={"historical_case": case}, evidence_ids=["E001"])


def source(title, date="2025-05-01", scope="history", fetched=False):
    return SimpleNamespace(
        title=title,
        content_text=title,
        snippet="",
        published_at=date,
        fetch_status="fetched" if fetched else "discovered",
        extra={"scope_status": scope, "date_provenance": "page_visible"},
    )


def test_history_requires_distinct_case_metadata_and_source():
    qualified = {
        "name": "某高校纠纷处置案",
        "institution": "某高校",
        "independent": True,
        "similarity": "复核机制",
        "difference": "主体与事实不同",
        "outcome": "已公开处分",
    }
    assert (
        independent_case_evidence(
            claim({}), {"E001": source("某高校公开复核结果")}, "武汉大学图书馆事件", "2026-01-01"
        )
        == set()
    )
    assert independent_case_evidence(
        claim(qualified), {"E001": source("某高校公开复核结果")}, "武汉大学图书馆事件", "2026-01-01"
    ) == {"E001"}
    assert (
        independent_case_evidence(
            claim(qualified),
            {"E001": source("某高校公开复核结果", date="2026-02-01")},
            "武汉大学图书馆事件",
            "2026-01-01",
        )
        == set()
    )
    assert (
        independent_case_evidence(
            claim(qualified),
            {"E001": source("别的事件，未提及机构")},
            "武汉大学图书馆事件",
            "2026-01-01",
        )
        == set()
    )
    same = {**qualified, "institution": "武汉大学"}
    assert (
        independent_case_evidence(
            claim(same),
            {"E001": source("武汉大学公开复核结果")},
            "武汉大学图书馆事件",
            "2026-01-01",
        )
        == set()
    )
    distinct_at_same_institution = {
        **same,
        "name": "武汉大学另一事件",
        "case_type": "analogous",
    }
    assert independent_case_evidence(
        claim(distinct_at_same_institution),
        {"E001": source("武汉大学另一事件公开复核结果")},
        "武汉大学图书馆事件",
        "2026-01-01",
    ) == {"E001"}
    related = {
        **distinct_at_same_institution,
        "case_type": "related_prior",
        "connection": "影响后续规则",
        "connection_quote": "该事件促成规则修订",
    }
    assert (
        independent_case_evidence(
            claim(related),
            {"E001": source("武汉大学另一事件公开复核结果")},
            "武汉大学图书馆事件",
            "2026-01-01",
        )
        == set()
    )
    assert independent_case_evidence(
        claim(related),
        {"E001": source("武汉大学另一事件：该事件促成规则修订", fetched=True)},
        "武汉大学图书馆事件",
        "2026-01-01",
    ) == {"E001"}
